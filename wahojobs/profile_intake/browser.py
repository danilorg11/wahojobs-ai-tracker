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
    LanguageValue,
    LANGUAGE_PROFICIENCIES,
    ProfileIntakeError,
    _FIELD_SPECS,
)
from wahojobs.profile_intake.runtime import (
    PROFILE_INTAKE_REVIEW_COLLECTIONS,
    PROFILE_INTAKE_REVIEW_ROUTE,
    PROFILE_INTAKE_REVIEW_STEPS,
    PROFILE_INTAKE_ROUTE,
    ProfileIntakeDocumentInput,
    ProfileIntakeAuthorityService,
    ProfileIntakeProcessingService,
    education_entry_values,
    managed_education_fact_indexes,
    managed_review_collection_fact_indexes,
    normalize_profile_intake_review_step,
    profile_intake_csrf_proof,
    review_collection_entries,
    review_value_for_form,
    update_editable_review,
)
from wahojobs.profiles.education_entries import (
    EDUCATION_ENTRY_KINDS,
    EDUCATION_ENTRY_STATUSES,
    MAX_EDUCATION_ENTRIES,
)
from wahojobs.profiles.preference_model import (
    MAX_COMPENSATION_EXPECTATIONS,
    ProfilePreferenceModelError,
    canonicalize_profile_preferences_v2,
    empty_profile_preferences_v2,
    preference_model_for_v2_editor,
    profile_preference_control_catalog_v2,
)
from wahojobs.profiles.seniority_presentation import (
    candidate_seniority_display_choices,
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
_PREFERENCE_COMPENSATION_MARKER = "preference_compensation_expectations_present"

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

_REVIEW_FIELD_LABELS = {
    "occupational_families": "Type of work",
    "professional_domains": "Areas of experience",
    "graduation_years": "Completion year",
}
_UNPAIRED_EDUCATION_LABELS = {
    "degrees": "Qualification or course (not linked to an entry)",
    "education_fields": "Field of study (not linked to an entry)",
    "institutions": "School or institution (not linked to an entry)",
    "graduation_years": "Completion year (not linked to an entry)",
    "education_status": "Education status (not linked to an entry)",
}
_COMPACT_FOUND_REVIEW_FIELDS = {
    "job_titles": "Job titles",
    "languages": "Languages",
    "skills": "Skills",
}
_INSUFFICIENT_CLASSIFICATION_VALUES = frozenset({"not_specified", "unknown"})

_SKIP_SUGGESTION_VALUE = "__leave_suggestion_out__"
_PRIMARY_CLASSIFICATION_CHOICES = {
    "education.education_level": (
        "no_degree",
        "high_school",
        "bachelor",
        "master",
        "doctorate",
        "not_specified",
    ),
    "experience.contribution_type": (
        "individual contributor",
        "management",
        "executive",
        "unknown",
    ),
}
_IMMEDIATE_PREFERENCE_DIMENSIONS = frozenset(
    {"employment_relationships", "workloads", "job_interests"}
)
_COMMON_JOB_INTEREST_CODES = (
    "customer_support",
    "administrative_support",
    "ai_training",
    "data_annotation",
    "search_evaluation",
    "software_engineering",
    "writing_editing",
    "translation_localization",
)
_COMMON_CURRENCIES = ("USD", "EUR", "GBP", "BRL", "CAD", "AUD", "INR", "JPY")
_MISSING_USER_FIELD_COPY = {
    "work_authorization": (
        "What work authorization do you have?",
        "For example, citizenship, a work visa, or no current authorization.",
    ),
    "eligible_countries": (
        "Where can you work?",
        "List countries where you are allowed to work, separated by commas.",
    ),
    "geographic_restrictions": (
        "Are there places where you cannot work?",
        "Add any location limits that should be respected.",
    ),
    "hard_constraints": (
        "Anything you cannot do?",
        "Add firm limits that a job must respect.",
    ),
    "soft_preferences": (
        "Anything you would rather avoid?",
        "Add preferences you could reconsider for the right opportunity.",
    ),
    "avoid_keywords": (
        "Words or topics you do not want in jobs",
        "Separate multiple words or topics with commas.",
    ),
    "excluded_domains": (
        "Job areas you do not want",
        "List industries or kinds of work you want to leave out.",
    ),
    "accessibility_constraints": (
        "Any accessibility needs?",
        "Share only what a job must provide or avoid for you to participate.",
    ),
}

_PROCESSING_SCRIPT = """(function(){var f=document.getElementById('profile-intake-upload');if(!f){return;}f.addEventListener('submit',function(){if(!f.checkValidity()){return;}var files=f.querySelectorAll('input[type=file]');if(!files[0].files.length&&!files[1].files.length){return;}var state=document.getElementById('profile-building-state');var content=document.getElementById('profile-upload-content');f.setAttribute('aria-busy','true');f.classList.add('is-processing');f.querySelector('button[type=submit]').disabled=true;content.hidden=true;state.hidden=false;state.focus();});}());"""
_PROCESSING_SCRIPT_HASH = base64.b64encode(
    hashlib.sha256(_PROCESSING_SCRIPT.encode("utf-8")).digest()
).decode("ascii")
_REVIEW_STATE_SCRIPT = """(function(){
var form=document.getElementById('profile-review-form');var autosave=document.getElementById('profile-review-autosave');var renew=document.getElementById('profile-review-renewal');var discard=document.getElementById('profile-review-discard');var status=document.getElementById('profile-review-save-status');var label=document.getElementById('profile-review-save-label');var retry=document.getElementById('profile-review-save-retry');var resume=document.getElementById('profile-review-save-resume');if(!form||!renew||!status||!label||!window.fetch){return;}var dirty=false;var saving=false;var current=null;var timer=null;var allowSubmit=false;var allowDiscard=false;var lastActivity=0;var lastRenewed=Date.now();function show(kind,text,canRetry,canResume){status.dataset.state=kind;label.textContent=text;if(retry){retry.hidden=!canRetry;}if(resume){resume.hidden=!canResume;}}function updateForm(target,version,proof){if(!target){return false;}var versionInput=target.querySelector('input[name=version]');var proofInput=target.querySelector('input[name=csrf]');if(!versionInput||!proofInput||!/^[0-9]+$/.test(version)||!proof){return false;}versionInput.value=version;proofInput.value=proof;return true;}function applyTokens(response){var version=response.headers.get('X-Wahojobs-Review-Version');return updateForm(form,version,response.headers.get('X-Wahojobs-CSRF-Save'))&&updateForm(autosave,version,response.headers.get('X-Wahojobs-CSRF-Autosave'))&&updateForm(renew,version,response.headers.get('X-Wahojobs-CSRF-Renew'))&&updateForm(discard,version,response.headers.get('X-Wahojobs-CSRF-Discard'));}function schedule(){if(!autosave){return;}dirty=true;window.clearTimeout(timer);timer=window.setTimeout(function(){saveNow(false);},1500);}function saveNow(keepalive){if(!autosave||!dirty){return current||Promise.resolve(true);}if(saving){return current;}saving=true;dirty=false;show('saving','Saving…',false,false);var data=new URLSearchParams(new FormData(form));data.set('action','autosave');data.set('version',autosave.querySelector('input[name=version]').value);data.set('csrf',autosave.querySelector('input[name=csrf]').value);current=window.fetch(form.getAttribute('action'),{method:'POST',body:data.toString(),credentials:'same-origin',keepalive:!!keepalive,headers:{'Content-Type':'application/x-www-form-urlencoded'}}).then(function(response){if(response.status===204&&applyTokens(response)){show('saved','Progress saved',false,false);return true;}dirty=true;if(response.status===409){show('conflict','Newer progress was saved in another tab.',false,true);}else if(response.status===410){show('expired','Your active review closed. Continue from your saved progress.',false,true);}else if(response.status===400){show('error','Check the unfinished details, then retry saving.',true,false);}else{show('error','We could not save your progress. Try again.',true,false);}return false;}).catch(function(){dirty=true;show('error','We could not save your progress. Try again.',true,false);return false;}).finally(function(){saving=false;current=null;});return current;}function flush(){return saveNow(false).then(function(ok){return ok&&dirty?flush():ok;});}function activity(){lastActivity=Date.now();}form.addEventListener('input',function(){activity();schedule();});form.addEventListener('change',function(){activity();schedule();});form.addEventListener('submit',function(event){if(allowSubmit||(!dirty&&!saving)){return;}event.preventDefault();var submitter=event.submitter;flush().then(function(ok){if(!ok){return;}allowSubmit=true;if(form.requestSubmit){if(submitter){form.requestSubmit(submitter);}else{form.requestSubmit();}}else{form.submit();}allowSubmit=false;});});if(discard){discard.addEventListener('submit',function(event){window.clearTimeout(timer);dirty=false;if(allowDiscard||!saving){return;}event.preventDefault();Promise.resolve(current).then(function(){dirty=false;allowDiscard=true;if(discard.requestSubmit){discard.requestSubmit();}else{discard.submit();}allowDiscard=false;});});}if(retry){retry.addEventListener('click',function(){saveNow(false);});}function tick(){var now=Date.now();if(saving||document.visibilityState!=='visible'||!lastActivity||now-lastActivity>360000||now-lastRenewed<300000){return;}lastRenewed=now;var data=new URLSearchParams(new FormData(renew));window.fetch(renew.getAttribute('action'),{method:'POST',body:data.toString(),credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded'}}).then(function(response){if(response.status===409){show('conflict','Newer progress was saved in another tab.',false,true);return;}if(response.status===410){show('expired','Your active review closed. Continue from your saved progress.',false,true);return;}if(!response.ok){return;}var remaining=Number(response.headers.get('X-Wahojobs-Review-Absolute-Seconds'));if(Number.isFinite(remaining)&&remaining<=600){show('warning','This review session closes in about '+Math.max(1,Math.ceil(remaining/60))+' minutes. Your saved progress will remain available.',false,false);}}).catch(function(){});}window.setInterval(tick,60000);window.addEventListener('pagehide',function(){if(dirty&&!saving){saveNow(true);}});show('saved',autosave?'Progress saved':'Review active',false,false);
}());"""
_REVIEW_STATE_SCRIPT = _REVIEW_STATE_SCRIPT.replace(
    "var form=document.getElementById('profile-review-form');",
    "var reviewSteps="
    + json.dumps(PROFILE_INTAKE_REVIEW_STEPS, ensure_ascii=True)
    + ";var form=document.getElementById('profile-review-form');",
).replace(
    "var lastActivity=0;var lastRenewed=Date.now();",
    "var lastActivity=0;var lastRenewed=Date.now();"
    "var step=form.querySelector('input[name=review_step]');",
).replace(
    "function activity(){lastActivity=Date.now();}form.addEventListener('input'",
    "function activity(){lastActivity=Date.now();}"
    "function setStep(value){if(!step||reviewSteps.indexOf(value)<0||step.value===value){return;}step.value=value;activity();schedule();}"
    "function rememberSection(event){var section=event.target.closest&&event.target.closest('section.review-section');if(section){setStep(section.id);}}"
    "Array.prototype.forEach.call(document.querySelectorAll('.review-progress a[href^=\"#review-\"]'),function(link){link.addEventListener('click',function(){setStep(link.getAttribute('href').slice(1));});});"
    "form.addEventListener('focusin',rememberSection);form.addEventListener('pointerdown',rememberSection);"
    "window.addEventListener('hashchange',function(){setStep(window.location.hash.slice(1));});"
    "form.addEventListener('input'",
)
_REVIEW_STATE_SCRIPT = _REVIEW_STATE_SCRIPT.replace(
    "if(response.status===204&&applyTokens(response)){show('saved'",
    "if(response.status===204&&applyTokens(response)){"
    "Array.prototype.forEach.call(form.querySelectorAll('[data-collection-new]'),function(item){item.removeAttribute('data-collection-new');});"
    "show('saved'",
).replace(
    "form.addEventListener('input',function(){activity();schedule();});",
    "function replaceIndex(item,oldIndex,newIndex){"
    "Array.prototype.forEach.call(item.querySelectorAll('[name]'),function(control){control.name=control.name.replace('_'+oldIndex+'_','_'+newIndex+'_');});"
    "Array.prototype.forEach.call(item.querySelectorAll('[id]'),function(control){control.id=control.id.replace('-'+oldIndex+'-','-'+newIndex+'-');});"
    "Array.prototype.forEach.call(item.querySelectorAll('[for]'),function(control){control.htmlFor=control.htmlFor.replace('-'+oldIndex+'-','-'+newIndex+'-');});"
    "item.dataset.index=String(newIndex);"
    "}"
    "function renumberNewItems(editor){"
    "var items=editor.querySelectorAll('[data-collection-item]');var fresh=editor.querySelectorAll('[data-collection-new]');var next=items.length-fresh.length;"
    "Array.prototype.forEach.call(fresh,function(item){replaceIndex(item,item.dataset.index,next);next+=1;});editor.dataset.nextIndex=String(next);"
    "var empty=editor.querySelector('.collection-empty');if(empty){empty.hidden=items.length!==0;}"
    "}"
    "form.addEventListener('click',function(event){"
    "var add=event.target.closest&&event.target.closest('[data-collection-add]');if(!add){return;}"
    "var editor=add.closest('[data-review-collection]');var template=editor&&editor.querySelector('template[data-collection-template]');var container=editor&&editor.querySelector('[data-collection-items]');"
    "var index=Number(editor&&editor.dataset.nextIndex);var limit=Number(editor&&editor.dataset.limit);if(!template||!container||!Number.isInteger(index)||index<0||index>=limit){return;}"
    "var fragment=template.content.cloneNode(true);var item=fragment.querySelector('[data-collection-item]');replaceIndex(item,'__INDEX__',index);container.appendChild(fragment);editor.dataset.nextIndex=String(index+1);"
    "var empty=editor.querySelector('.collection-empty');if(empty){empty.hidden=true;}var input=item.querySelector('input:not([type=checkbox]),select');if(input){input.focus();}activity();"
    "});"
    "form.addEventListener('change',function(event){"
    "if(!event.target.matches||!event.target.matches('[data-collection-remove]')||!event.target.checked){return;}var item=event.target.closest('[data-collection-new]');if(!item){return;}var editor=item.closest('[data-review-collection]');item.remove();renumberNewItems(editor);"
    "});"
    "form.addEventListener('input',function(){activity();schedule();});",
)
_REVIEW_STATE_SCRIPT_HASH = base64.b64encode(
    hashlib.sha256(_REVIEW_STATE_SCRIPT.encode("utf-8")).digest()
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
            if preflight == "checkpoint_available":
                try:
                    progress = self._processing.saved_progress(grant)
                except ProfileIntakeError as exc:
                    return _failure(_processing_error_code(exc.code))
                if progress is None:
                    return _failure("expired_checkpoint")
                return _form_page_response(
                    HTTPStatus.OK,
                    _resume_page(
                        progress,
                        profile_intake_csrf_proof(csrf_secret, "continue"),
                        profile_intake_csrf_proof(csrf_secret, "discard_saved"),
                    ),
                )
            if preflight != "eligible":
                return _failure(_preflight_error_code(preflight))
            proof = profile_intake_csrf_proof(csrf_secret, "upload")
            return _form_page_response(
                HTTPStatus.OK,
                _upload_page(proof),
                script_sha256=_PROCESSING_SCRIPT_HASH,
            )
        if _header_values(headers, "content-type") == (
            "application/x-www-form-urlencoded",
        ):
            form = _parse_review_form(headers, body_stream)
            if form is None or set(form) != {"action", "csrf"}:
                return _failure("invalid_request")
            action = _single(form, "action")
            proof = _single(form, "csrf")
            if action not in {"continue", "discard_saved"}:
                return _failure("invalid_request")
            grant, failure = self._authorize(
                method="POST",
                route=PROFILE_INTAKE_ROUTE,
                authentication_input=authentication_input,
                session_token=session_token,
                csrf_secret=csrf_secret,
                action=action,
                proof=proof,
            )
            if failure is not None:
                return failure
            if action == "discard_saved":
                try:
                    state = self._processing.discard_saved(grant)
                except ProfileIntakeError as exc:
                    return _failure(_processing_error_code(exc.code))
                if state not in {"discarded", "gone"}:
                    return _failure("unavailable")
                return _response(
                    HTTPStatus.SEE_OTHER,
                    _message_page(
                        "Saved progress discarded",
                        "You can start again whenever you are ready.",
                    ),
                    extra_headers=(("Location", PROFILE_INTAKE_ROUTE),),
                )
            try:
                reference, snapshot = self._processing.resume_saved(grant)
            except ProfileIntakeError as exc:
                return _failure(_processing_error_code(exc.code))
            return _response(
                HTTPStatus.SEE_OTHER,
                _message_page(
                    "Saved progress ready",
                    "Continue reviewing your profile.",
                ),
                extra_headers=((
                    "Location",
                    PROFILE_INTAKE_REVIEW_ROUTE
                    + "?"
                    + urlencode({"draft": reference})
                    + "#"
                    + snapshot.review_step,
                ),),
            )
        try:
            preflight = self._processing.preflight(grant)
        except ProfileIntakeError as exc:
            return _failure(_processing_error_code(exc.code))
        if preflight != "eligible":
            if preflight == "checkpoint_available":
                return _failure("checkpoint_available")
            return _failure(_preflight_error_code(preflight))
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
                script_sha256=_REVIEW_STATE_SCRIPT_HASH,
                script_connect_self=True,
            )
        form = _parse_review_form(headers, body_stream)
        if form is None:
            return _failure("invalid_review")
        action = _single(form, "action")
        raw_version = _single(form, "version")
        proof = _single(form, "csrf")
        if (
            action not in {"update", "autosave", "renew", "cancel", "save"}
            or raw_version is None
            or not raw_version.isdigit()
            or int(raw_version) < 1
        ):
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
        if action == "renew":
            if set(form) != {"action", "version", "csrf"}:
                return _failure("invalid_review")
            state, renewal = self._processing.renew(
                reference,
                grant,
                expected_version=version,
            )
            if state == "stale":
                return _failure("stale_review")
            if state not in {"renewed", "rate_limited"} or renewal is None:
                return _failure("expired_draft")
            _snapshot, idle_seconds, absolute_seconds = renewal
            return _response(
                HTTPStatus.NO_CONTENT,
                "",
                extra_headers=(
                    ("X-Wahojobs-Review-Idle-Seconds", str(idle_seconds)),
                    ("X-Wahojobs-Review-Absolute-Seconds", str(absolute_seconds)),
                ),
            )
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
            review_step = normalize_profile_intake_review_step(
                _single(form, "review_step")
            )
            review = _review_from_form(
                snapshot.review,
                form,
                allow_pending=action == "autosave",
            )
        except ProfileIntakeError:
            return _failure("invalid_review")
        if action == "autosave":
            try:
                _save_state, updated = self._processing.autosave(
                    reference,
                    grant,
                    expected_version=version,
                    review=review,
                    review_step=review_step,
                )
            except ProfileIntakeError as exc:
                return _failure(_save_error_code(exc.code))
            return _response(
                HTTPStatus.NO_CONTENT,
                "",
                extra_headers=_review_state_headers(
                    csrf_secret,
                    reference,
                    updated.version,
                ),
            )
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
        state, _updated = self._processing.update(
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


def _review_from_form(review, form, *, allow_pending=False):
    if type(allow_pending) is not bool:
        raise ProfileIntakeError("invalid_review_submission")
    expected = {"action", "version", "csrf"}
    if "review_step" in form:
        expected.add("review_step")
    collection_updates, collection_fields = _review_collections_from_form(
        review,
        form,
    )
    expected.update(collection_fields)
    education_updates, education_fields = _education_entries_from_form(
        review,
        form,
    )
    expected.update(education_fields)
    managed_indexes = (
        managed_review_collection_fact_indexes(review)
        if collection_updates is not None
        else frozenset()
    )
    if education_updates is not None:
        managed_indexes = managed_indexes | managed_education_fact_indexes(review)
    values = []
    decisions = []
    for index, fact in enumerate(review.facts):
        if index in managed_indexes:
            values.append(review_value_for_form(fact.value))
            decisions.append(fact.decision)
            continue
        value_name = f"fact_{index}_value"
        decision_name = f"fact_{index}_decision"
        expected.add(decision_name)
        value = _single(form, value_name)
        decision = _single(form, decision_name)
        if value is None:
            if (
                not allow_pending
                or not _uses_compact_suggestion_choice(fact)
                or fact.decision != "pending"
                or decision != "accept"
            ):
                raise ProfileIntakeError("invalid_review_submission")
            value = review_value_for_form(fact.value)
            decision = "pending"
        else:
            expected.add(value_name)
        if decision is None:
            raise ProfileIntakeError("invalid_review_submission")
        if value == _SKIP_SUGGESTION_VALUE:
            if not _uses_compact_suggestion_choice(fact):
                raise ProfileIntakeError("invalid_review_submission")
            value = review_value_for_form(fact.value)
            decision = "reject"
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
        collection_updates,
        education_updates,
    )


def _education_entries_from_form(review, form):
    names = {name for name in form if name.startswith("review_education_")}
    if not names:
        return None, set()
    suffixes = (
        "kind",
        "qualification",
        "field",
        "institution",
        "status",
        "completion_year",
        "remove",
    )
    pattern = re.compile(
        r"^review_education_([0-9]{1,3})_(" + "|".join(suffixes) + r")$"
    )
    indexes = set()
    matched = set()
    for name in names:
        match = pattern.fullmatch(name)
        if match is not None:
            indexes.add(int(match.group(1)))
            matched.add(name)
    if matched != names or not indexes or indexes != set(range(max(indexes) + 1)):
        raise ProfileIntakeError("invalid_review_submission")
    existing_count = len(education_entry_values(review))
    item_count = max(indexes) + 1
    if not existing_count <= item_count <= MAX_EDUCATION_ENTRIES:
        raise ProfileIntakeError("invalid_review_submission")
    submitted = set()
    updates = []
    for index in range(item_count):
        prefix = f"review_education_{index}_"
        values = []
        for suffix in suffixes[:-1]:
            name = prefix + suffix
            value = _single(form, name)
            if value is None:
                raise ProfileIntakeError("invalid_review_submission")
            submitted.add(name)
            values.append(value)
        remove_name = prefix + "remove"
        remove = _single(form, remove_name)
        if remove is not None:
            submitted.add(remove_name)
            if remove != "remove":
                raise ProfileIntakeError("invalid_review_submission")
        updates.append((*values, "remove" if remove == "remove" else "keep"))
    return tuple(updates), submitted


def _review_collections_from_form(review, form):
    collection_names = {
        name for name in form if name.startswith("review_collection_")
    }
    if not collection_names:
        return None, set()
    updates = {}
    submitted_fields = set()
    matched_fields = set()
    for collection_id, spec in PROFILE_INTAKE_REVIEW_COLLECTIONS.items():
        suffixes = (
            ("language", "proficiency", "locale", "remove")
            if spec["kind"] == "language"
            else ("value", "remove")
        )
        pattern = re.compile(
            rf"^review_collection_{re.escape(collection_id)}_([0-9]{{1,3}})_"
            rf"({'|'.join(suffixes)})$"
        )
        indexes = set()
        for name in collection_names:
            match = pattern.fullmatch(name)
            if match is not None:
                indexes.add(int(match.group(1)))
                matched_fields.add(name)
        existing_count = len(review_collection_entries(review, collection_id))
        if indexes:
            if indexes != set(range(max(indexes) + 1)):
                raise ProfileIntakeError("invalid_review_submission")
            item_count = max(indexes) + 1
        else:
            item_count = 0
        if not existing_count <= item_count <= spec["limit"]:
            raise ProfileIntakeError("invalid_review_submission")
        items = []
        for index in range(item_count):
            prefix = f"review_collection_{collection_id}_{index}_"
            remove_name = prefix + "remove"
            remove = _single(form, remove_name)
            if remove is not None:
                submitted_fields.add(remove_name)
                if remove != "remove":
                    raise ProfileIntakeError("invalid_review_submission")
            decision = "remove" if remove == "remove" else "keep"
            if spec["kind"] == "language":
                names = tuple(prefix + suffix for suffix in ("language", "proficiency", "locale"))
                values = tuple(_single(form, name) for name in names)
                if any(value is None for value in values):
                    raise ProfileIntakeError("invalid_review_submission")
                submitted_fields.update(names)
                items.append((*values, decision))
            else:
                value_name = prefix + "value"
                value = _single(form, value_name)
                if value is None:
                    raise ProfileIntakeError("invalid_review_submission")
                submitted_fields.add(value_name)
                items.append((value, decision))
        updates[collection_id] = tuple(items)
    if matched_fields != collection_names:
        raise ProfileIntakeError("invalid_review_submission")
    return updates, submitted_fields


def _collection_form_values_for_review(review):
    """Return the sole browser representation of current typed collections."""

    fields = {}
    for collection_id, spec in PROFILE_INTAKE_REVIEW_COLLECTIONS.items():
        for index, entry in enumerate(review_collection_entries(review, collection_id)):
            prefix = f"review_collection_{collection_id}_{index}_"
            value = entry["value"]
            if spec["kind"] == "language":
                if type(value) is not LanguageValue:
                    raise ProfileIntakeError("invalid_review_submission")
                fields[prefix + "language"] = value.language
                fields[prefix + "proficiency"] = value.proficiency or ""
                fields[prefix + "locale"] = value.locale or ""
            else:
                fields[prefix + "value"] = review_value_for_form(value)
            if entry["decision"] == "remove":
                fields[prefix + "remove"] = "remove"
    return fields


def _education_form_values_for_review(review):
    """Return the sole browser representation of structured education."""

    fields = {}
    for index, entry in enumerate(education_entry_values(review)):
        prefix = f"review_education_{index}_"
        value = entry["value"]
        fields.update(
            {
                prefix + "kind": value["kind"],
                prefix + "qualification": value["qualification"],
                prefix + "field": value["field"],
                prefix + "institution": value["institution"],
                prefix + "status": value["status"],
                prefix + "completion_year": (
                    ""
                    if value["completion_year"] is None
                    else str(value["completion_year"])
                ),
            }
        )
        if entry["decision"] == "remove":
            fields[prefix + "remove"] = "remove"
    return fields


def _uses_compact_suggestion_choice(fact):
    spec = _FIELD_SPECS.get(getattr(fact, "field_path", None))
    return bool(
        getattr(fact, "suggested", False)
        and getattr(fact, "conflict_group", None) is None
        and spec is not None
        and spec.kind == "enum"
        and not spec.multiple
    )


def _preference_model_from_form(form):
    """Build the sole authoritative model from closed server-owned controls."""

    if type(form) is not dict:
        raise ProfileIntakeError("invalid_review_submission")
    catalog = profile_preference_control_catalog_v2()
    model = empty_profile_preferences_v2()
    allowed_checkbox_fields = {}
    for dimension in catalog["dimensions"]:
        path = dimension["path"]
        for choice in dimension["choices"]:
            field_name = _preference_choice_field(path, choice["code"])
            allowed_checkbox_fields[field_name] = (path, choice["code"])

    submitted = {_PREFERENCE_COMPENSATION_MARKER}
    if _single(form, _PREFERENCE_COMPENSATION_MARKER) != "present":
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

    expectations, expectation_fields = _compensation_expectations_from_form(form)
    model["compensation_expectations"] = expectations
    submitted.update(expectation_fields)
    try:
        return canonicalize_profile_preferences_v2(model), submitted
    except ProfilePreferenceModelError:
        raise ProfileIntakeError("invalid_review_submission") from None


def _compensation_expectations_from_form(form):
    names = {name for name in form if name.startswith("preference_compensation_")}
    names.discard(_PREFERENCE_COMPENSATION_MARKER)
    if not names:
        return [], set()
    suffixes = ("minimum_kind", "amount", "currency", "period", "remove")
    pattern = re.compile(
        r"^preference_compensation_([0-9]{1,2})_(" + "|".join(suffixes) + r")$"
    )
    indexes = set()
    matched = set()
    for name in names:
        match = pattern.fullmatch(name)
        if match is not None:
            indexes.add(int(match.group(1)))
            matched.add(name)
    if (
        matched != names
        or not indexes
        or indexes != set(range(max(indexes) + 1))
        or len(indexes) > MAX_COMPENSATION_EXPECTATIONS
    ):
        raise ProfileIntakeError("invalid_review_submission")
    expectations = []
    submitted = set()
    for index in range(max(indexes) + 1):
        prefix = f"preference_compensation_{index}_"
        values = {}
        for suffix in suffixes[:-1]:
            name = prefix + suffix
            value = _single(form, name)
            if value is None:
                raise ProfileIntakeError("invalid_review_submission")
            submitted.add(name)
            values[suffix] = value
        remove_name = prefix + "remove"
        remove = _single(form, remove_name)
        if remove is not None:
            submitted.add(remove_name)
            if remove != "remove":
                raise ProfileIntakeError("invalid_review_submission")
        else:
            expectations.append(values)
    return expectations, submitted


def _preference_choice_field(path, code):
    name = "preference_" + "_".join((*path, code))
    if _FIELD_NAME.fullmatch(name) is None:
        raise ProfileIntakeError("invalid_review_submission")
    return name


def _preference_form_values_for_model(model):
    """Return the exact browser fields for tests and server-built replays."""

    canonical = preference_model_for_v2_editor(model)
    fields = {_PREFERENCE_COMPENSATION_MARKER: "present"}
    for dimension in profile_preference_control_catalog_v2()["dimensions"]:
        path = dimension["path"]
        parent = canonical
        for part in path:
            parent = parent[part]
        for code in parent:
            fields[_preference_choice_field(path, code)] = "selected"
    for index, compensation in enumerate(canonical["compensation_expectations"]):
        prefix = f"preference_compensation_{index}_"
        fields.update(
            {
                prefix + "minimum_kind": compensation["minimum_kind"],
                prefix + "amount": compensation["amount"],
                prefix + "currency": compensation["currency"],
                prefix + "period": compensation["period"],
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
            if name not in {"csrf", "review_step"}
            and type(values) is list
            and len(values) == 1
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


def _review_state_headers(csrf_secret, reference, version):
    return (
        ("X-Wahojobs-Review-Version", str(version)),
        (
            "X-Wahojobs-CSRF-Save",
            profile_intake_csrf_proof(
                csrf_secret,
                "save",
                draft_reference=reference,
                version=version,
            ),
        ),
        (
            "X-Wahojobs-CSRF-Autosave",
            profile_intake_csrf_proof(
                csrf_secret,
                "autosave",
                draft_reference=reference,
                version=version,
            ),
        ),
        (
            "X-Wahojobs-CSRF-Renew",
            profile_intake_csrf_proof(
                csrf_secret,
                "renew",
                draft_reference=reference,
                version=version,
            ),
        ),
        (
            "X-Wahojobs-CSRF-Discard",
            profile_intake_csrf_proof(
                csrf_secret,
                "cancel",
                draft_reference=reference,
                version=version,
            ),
        ),
    )


def _resume_page(progress, continue_proof, discard_proof):
    age = _saved_age_copy(progress.age_seconds)
    body = f"""
    {_authenticated_navigation()}
    <section class='profile-header intake-hero resume-intake-hero'>
      <p class='eyebrow'>Your Wahojobs profile</p>
      <h1>Continue building your profile</h1>
      <p class='hero-lede'>Pick up where you left off—your reviewed details and job preferences are ready.</p>
      <div class='saved-progress-summary' role='status'>
        <span class='saved-progress-mark' aria-hidden='true'>&#10003;</span>
        <div><strong>Progress saved</strong><p>Last saved {_safe_text(age)}. Your progress is saved for 7 days after your last saved change.</p></div>
      </div>
      <form method='post' action='{PROFILE_INTAKE_ROUTE}' class='resume-primary-action'>
        <input type='hidden' name='action' value='continue'><input type='hidden' name='csrf' value='{_safe_text(continue_proof)}'>
        <button type='submit'>Continue</button>
      </form>
      <p class='muted'>You will continue without uploading your documents again.</p>
      <details class='resume-discard-disclosure'>
        <summary>Start over instead</summary>
        <div class='disclosure-body'><p>Starting over permanently removes this saved review. It will not happen just because you leave this page.</p>
          <form method='post' action='{PROFILE_INTAKE_ROUTE}'>
            <input type='hidden' name='action' value='discard_saved'><input type='hidden' name='csrf' value='{_safe_text(discard_proof)}'>
            <button class='button-quiet destructive-action' type='submit'>Discard saved progress and start over</button>
          </form>
        </div>
      </details>
    </section>
    """
    return _page("Continue your profile", body)


def _saved_age_copy(seconds):
    if seconds < 60:
        return "just now"
    if seconds < 3_600:
        count = max(1, seconds // 60)
        return f"{count} minute{'s' if count != 1 else ''} ago"
    if seconds < 86_400:
        count = max(1, seconds // 3_600)
        return f"{count} hour{'s' if count != 1 else ''} ago"
    count = max(1, seconds // 86_400)
    return f"{count} day{'s' if count != 1 else ''} ago"


def _upload_page(proof):
    body = f"""
    {_authenticated_navigation()}
    <section class='profile-header intake-hero'>
      <p class='eyebrow'>Create your Wahojobs profile</p>
      <h1>Start with what you already have</h1>
      <p class='hero-lede'>Add a resume, a LinkedIn PDF, or both. Wahojobs will organize a profile draft for you to review.</p>
      <p class='reassurance-line'><span aria-hidden='true'>&#10003;</span> You review every detail before your profile is created.</p>
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
        <p class='reassurance-line'><span aria-hidden='true'>&#10003;</span> No profile is created before you review and confirm it.</p>
      </section>
    </form>
    <script>{_PROCESSING_SCRIPT}</script>
    """
    return _page("Create your profile", body)


def _review_page(reference, snapshot, csrf_secret, *, save_enabled=False):
    fact_fields = []
    education_fact_indexes = managed_education_fact_indexes(snapshot.review)
    for index, fact in enumerate(snapshot.review.facts):
        if index in education_fact_indexes:
            continue
        label = (
            _UNPAIRED_EDUCATION_LABELS.get(fact.review_field)
            if snapshot.review.education_entries
            else None
        ) or _REVIEW_FIELD_LABELS.get(
            fact.review_field,
            fact.review_field.replace("_", " ").title(),
        )
        raw_value = review_value_for_form(fact.value)
        source_label = _review_source_label(fact)
        if _uses_compact_suggestion_choice(fact):
            choice = (
                f"<input type='hidden' name='fact_{index}_decision' value='accept'>"
            )
            badge = source_label.replace("Found in", "Suggested from", 1)
        elif fact.suggested:
            choice = (
                f"<label class='decision-field'><span>Add this to your profile?</span><select name='fact_{index}_decision'>"
                f"<option value='pending'{' selected' if fact.decision == 'pending' else ''}>Choose an option</option>"
                f"<option value='accept'{' selected' if fact.decision == 'accept' else ''}>Add it</option>"
                f"<option value='reject'{' selected' if fact.decision == 'reject' else ''}>Leave it out</option></select></label>"
            )
            badge = source_label.replace("Found in", "Suggested from", 1)
        else:
            choice = (
                f"<label class='decision-field'><span>Include this in your profile?</span><select name='fact_{index}_decision'>"
                f"<option value='keep'{' selected' if fact.decision == 'keep' else ''}>Include it</option>"
                f"<option value='remove'{' selected' if fact.decision == 'remove' else ''}>Leave it out</option></select></label>"
            )
            badge = source_label
        if fact.conflict_group is not None:
            badge = f"{source_label} — conflicts with another document"
        value_control = _review_fact_value_control(index, fact, raw_value, label)
        card = (
            f"<article class='profile-group fact-card'><p class='fact-meta'>{_safe_text(badge)}</p>"
            f"{value_control}{choice}</article>"
        )
        compact_item = (
            f"<div class='fact-group-item'><p class='fact-meta'>{_safe_text(badge)}</p>"
            f"{value_control}{choice}</div>"
        )
        fact_fields.append((fact, card, compact_item))
    missing = []
    existing_inputs = dict(snapshot.review.user_inputs)
    for name in snapshot.review.missing_user_fields:
        label, help_text = _MISSING_USER_FIELD_COPY.get(
            name,
            (name.replace("_", " ").title(), "Add this only if it matters to your search."),
        )
        missing.append(
            f"<label class='review-field candidate-input'><span>{_safe_text(label)}</span>"
            f"<small>{_safe_text(help_text)}</small>"
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
    renewal_proof = profile_intake_csrf_proof(
        csrf_secret,
        "renew",
        draft_reference=reference,
        version=snapshot.version,
    )
    autosave_proof = profile_intake_csrf_proof(
        csrf_secret,
        "autosave",
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
        "Your progress is saved as you review. Your profile is created only when you choose Find my matches."
        if save_enabled
        else "You can update this preview, but it cannot be saved here."
    )
    autosave_form = (
        f"<form id='profile-review-autosave' method='post' action='{target}' hidden>"
        f"<input type='hidden' name='action' value='autosave'><input type='hidden' name='version' value='{snapshot.version}'><input type='hidden' name='csrf' value='{autosave_proof}'>"
        "</form>"
        if save_enabled
        else ""
    )
    found_cards = _render_found_fact_cards(fact_fields)
    collection_controls = (
        _render_education_entries(snapshot.review)
        + _render_review_collections(snapshot.review)
    )
    suggestion_cards = "".join(
        card
        for fact, card, _compact_item in fact_fields
        if fact.suggested and fact.conflict_group is None
    )
    if not suggestion_cards:
        suggestion_cards = (
            "<p class='empty-inline'>No suggestions need your confirmation.</p>"
        )
    conflict_cards = "".join(
        card
        for fact, card, _compact_item in fact_fields
        if fact.conflict_group is not None
    )
    conflict_section = (
        "<div class='review-subsection'><h3>Sources disagree — please confirm</h3>"
        "<p class='muted'>Choose at most one value for each disagreement, edit it "
        "if needed, or leave the alternatives out.</p>"
        f"<div class='profile-grid'>{conflict_cards}</div></div>"
        if conflict_cards
        else ""
    )
    answered_user_fields = sum(
        bool(existing_inputs.get(name, "").strip())
        for name in snapshot.review.missing_user_fields
    )
    missing_summary = (
        f"{answered_user_fields} answered"
        if answered_user_fields
        else "Optional details about where and how you can work"
    )
    missing_section = (
        f"<details class='preference-disclosure user-details-disclosure'{' open' if answered_user_fields else ''}>"
        "<summary><span>A few details only you can answer"
        f"<small>{_safe_text(missing_summary)}</small></span></summary>"
        "<div class='disclosure-body'><p class='muted'>Add anything that matters to your search. Leave a field blank if it does not apply.</p>"
        f"<div class='review-grid'>{''.join(missing)}</div></div></details>"
        if missing
        else ""
    )
    body = f"""
    {_authenticated_navigation()}
    <section class='profile-header intake-hero intake-review-hero'><p class='eyebrow'>Your Wahojobs profile draft</p><h1>Review it and make it yours</h1>
      <p class='hero-lede'>We organized what your documents say. You decide what belongs in your profile.</p>
      <p class='reassurance-line'><span aria-hidden='true'>&#10003;</span> Your progress is saved as you review. No profile is created until you finish.</p>
      <nav class='review-progress' aria-label='Profile review steps'><ol>
        <li><a href='#review-found'><span>1</span>What we found</a></li>
        <li><a href='#review-suggestions'><span>2</span>Confirm suggestions</a></li>
        <li><a href='#review-preferences'><span>3</span>What you want</a></li>
        <li><a href='#review-finish'><span>4</span>Find matches</a></li>
      </ol></nav>
    </section>
    {issue_note}
    <p id='profile-review-lifetime' class='intake-callout review-expiry-warning' role='status' aria-live='polite' hidden></p>
    <div id='profile-review-save-status' class='review-save-status' data-state='saved' role='status' aria-live='polite'>
      <span class='save-status-dot' aria-hidden='true'></span><span id='profile-review-save-label'>{'Progress saved' if save_enabled else 'Review active'}</span>
      <button id='profile-review-save-retry' class='button-quiet' type='button' hidden>Retry</button>
      <a id='profile-review-save-resume' href='{PROFILE_INTAKE_ROUTE}' hidden>Continue saved progress</a>
    </div>
    <form id='profile-review-form' class='profile-review-form intake-review-form' method='post' action='{target}'>
      <input type='hidden' name='action' value='{primary_action}'><input type='hidden' name='version' value='{snapshot.version}'><input type='hidden' name='csrf' value='{primary_proof}'><input type='hidden' name='review_step' value='{_safe_text(snapshot.review_step)}'>
      <section class='review-section' id='review-found' aria-labelledby='review-found-title'><div class='section-heading'><p class='eyebrow'>Step 1 of 4</p><h2 id='review-found-title'>What we found</h2><p>Check the details taken directly from your documents, then add anything useful that was missing.</p></div><div class='profile-grid'>{found_cards}</div>{collection_controls}</section>
      <section class='review-section' id='review-suggestions' aria-labelledby='review-suggestions-title'><div class='section-heading'><p class='eyebrow'>Step 2 of 4</p><h2 id='review-suggestions-title'>Confirm our suggestions</h2><p>These classifications can make your profile more useful. See every available choice and select what feels accurate.</p></div><div class='profile-grid'>{suggestion_cards}</div>{conflict_section}</section>
      <section class='review-section' id='review-preferences' aria-labelledby='review-preferences-title'><div class='section-heading'><p class='eyebrow'>Step 3 of 4</p><h2 id='review-preferences-title'>What are you looking for?</h2><p>Choose all the options you would consider. Each group is separate, so choices such as freelance and full-time can work together.</p><p class='preference-open-note'>Leave a group blank when you are open to all of its options.</p></div>{_render_preference_controls(snapshot.review.preference_model)}
        {missing_section}</section>
      <section class='review-section finish-section' id='review-finish' aria-labelledby='review-finish-title'><div class='finish-panel'><p class='eyebrow'>Step 4 of 4</p><h2 id='review-finish-title'>Review &amp; find matches</h2><p>When everything looks right, see the opportunities that fit the profile you confirmed. You can update your profile later.</p><div class='finish-actions'><button type='submit'>{primary_label}</button><span class='muted'>{persistence_note}</span></div></div></section>
    </form>
    <form id='profile-review-discard' class='intake-cancel-form' method='post' action='{target}'>
      <input type='hidden' name='action' value='cancel'><input type='hidden' name='version' value='{snapshot.version}'><input type='hidden' name='csrf' value='{cancel_proof}'><button class='button-quiet destructive-action' type='submit'>Discard saved progress</button>
    </form>
    {autosave_form}
    <form id='profile-review-renewal' method='post' action='{target}' hidden>
      <input type='hidden' name='action' value='renew'><input type='hidden' name='version' value='{snapshot.version}'><input type='hidden' name='csrf' value='{renewal_proof}'>
    </form>
    <script>{_REVIEW_STATE_SCRIPT}</script>
    """
    return _page("Review your profile", body)


def _render_found_fact_cards(fact_fields):
    grouped_counts = {}
    for fact, _card, _compact_item in fact_fields:
        if (
            fact.suggested
            or fact.conflict_group is not None
            or _is_managed_collection_fact(fact)
        ):
            continue
        if fact.review_field in _COMPACT_FOUND_REVIEW_FIELDS:
            grouped_counts[fact.review_field] = (
                grouped_counts.get(fact.review_field, 0) + 1
            )

    grouped_items = {}
    ordered = []
    for fact, card, compact_item in fact_fields:
        if (
            fact.suggested
            or fact.conflict_group is not None
            or _is_managed_collection_fact(fact)
        ):
            continue
        if grouped_counts.get(fact.review_field, 0) < 2:
            ordered.append(("card", card))
            continue
        if fact.review_field not in grouped_items:
            grouped_items[fact.review_field] = []
            ordered.append(("group", fact.review_field))
        grouped_items[fact.review_field].append(compact_item)

    rendered = []
    for kind, value in ordered:
        if kind == "card":
            rendered.append(value)
            continue
        items = grouped_items[value]
        label = _COMPACT_FOUND_REVIEW_FIELDS[value]
        count_label = "1 item" if len(items) == 1 else f"{len(items)} items"
        rendered.append(
            "<article class='profile-group fact-card fact-group-card'>"
            "<div class='fact-group-heading'>"
            f"<h3>{_safe_text(label)}</h3><span>{_safe_text(count_label)}</span>"
            "</div><div class='fact-group-items'>"
            + "".join(items)
            + "</div></article>"
        )
    return "".join(rendered)


def _is_managed_collection_fact(fact):
    return bool(
        not fact.suggested
        and fact.conflict_group is None
        and any(
            fact.field_path in spec["paths"]
            for spec in PROFILE_INTAKE_REVIEW_COLLECTIONS.values()
        )
    )


def _render_review_collections(review):
    sections = []
    for collection_id, spec in PROFILE_INTAKE_REVIEW_COLLECTIONS.items():
        entries = review_collection_entries(review, collection_id)
        items = "".join(
            _render_review_collection_item(collection_id, spec, index, entry)
            for index, entry in enumerate(entries)
        )
        next_index = len(entries)
        template_item = _render_review_collection_item(
            collection_id,
            spec,
            "__INDEX__",
            {
                "origin": "user",
                "value": LanguageValue("", None, None)
                if spec["kind"] == "language"
                else "",
                "decision": "keep",
                "source_attributions": (),
            },
            template=True,
        )
        empty = (
            "<p class='collection-empty'>Nothing listed yet. Add an item if it belongs in your profile.</p>"
            if not entries
            else ""
        )
        noun = {
            "skills": "skill",
            "job_titles": "job title",
            "languages": "language",
        }[collection_id]
        help_text = {
            "skills": "Add practical skills, tools, or subject knowledge. Similar entries are kept only once.",
            "job_titles": "Add roles you have held. Job interests are chosen separately later.",
            "languages": "Add each language separately. Leave proficiency blank when you do not want to state one.",
        }[collection_id]
        sections.append(
            f"<fieldset class='review-collection {collection_id}-collection' data-review-collection='{collection_id}' data-next-index='{next_index}' data-limit='{spec['limit']}'>"
            f"<legend>{_safe_text(spec['title'])}</legend><p class='muted'>{_safe_text(help_text)}</p>"
            f"{empty}<div class='review-collection-items' data-collection-items>{items}</div>"
            f"<button class='button-quiet collection-add' type='button' data-collection-add>Add another {_safe_text(noun)}</button>"
            f"<template data-collection-template>{template_item}</template></fieldset>"
        )
    return "<div class='review-collections' aria-label='Profile lists'>" + "".join(sections) + "</div>"


_PRIMARY_EDUCATION_ENTRY_KINDS = (
    "not_specified",
    "high_school",
    "technical",
    "associate",
    "bachelor",
    "master",
    "doctorate",
    "professional_degree",
)
_EDUCATION_KIND_LABELS = {
    "not_specified": "Not specified",
    "high_school": "High school",
    "technical": "Technical or vocational",
    "associate": "Associate degree",
    "bachelor": "Bachelor's degree",
    "master": "Master's degree",
    "doctorate": "Doctorate",
    "professional_degree": "Professional degree",
    "advanced_degree": "Advanced degree",
    "professional": "Professional qualification",
    "phd": "PhD",
}
_EDUCATION_STATUS_LABELS = {
    "completed": "Completed",
    "in_progress": "In progress",
    "not_specified": "Not specified",
    "unknown": "Not specified",
}


def _render_education_entries(review):
    entries = education_entry_values(review)
    items = "".join(
        _render_education_entry(index, entry)
        for index, entry in enumerate(entries)
    )
    template = _render_education_entry(
        "__INDEX__",
        {
            "origin": "user",
            "value": {
                "kind": "not_specified",
                "qualification": "",
                "field": "",
                "institution": "",
                "status": "not_specified",
                "completion_year": None,
            },
            "source_attributions": (),
            "decision": "keep",
        },
        template=True,
    )
    empty = (
        "<p class='collection-empty'>Nothing listed yet. Add your education if you would like it in your profile.</p>"
        if not entries
        else ""
    )
    return (
        "<div class='review-collections education-review-collections' aria-label='Education'>"
        f"<fieldset class='review-collection education-collection' data-review-collection='education' data-next-index='{len(entries)}' data-limit='{MAX_EDUCATION_ENTRIES}'>"
        "<legend>Education</legend><p class='muted'>Keep each school, degree, or course together so its status and completion year are clear.</p>"
        f"{empty}<div class='review-collection-items' data-collection-items>{items}</div>"
        "<button class='button-quiet collection-add' type='button' data-collection-add>Add another education entry</button>"
        f"<template data-collection-template>{template}</template></fieldset></div>"
    )


def _render_education_entry(index, entry, *, template=False):
    value = entry["value"]
    prefix = f"review_education_{index}_"
    item_id = f"review-education-{index}"
    removed = entry["decision"] == "remove"
    source = (
        "Added by you"
        if entry["origin"] == "user"
        else _collection_source_label(entry["source_attributions"])
    )
    kind_values = list(_PRIMARY_EDUCATION_ENTRY_KINDS)
    if value["kind"] not in kind_values:
        kind_values.append(value["kind"])
    kind_options = "".join(
        f"<option value='{_safe_text(code)}'{' selected' if code == value['kind'] else ''}>{_safe_text(_EDUCATION_KIND_LABELS.get(code, code.replace('_', ' ').title()))}</option>"
        for code in kind_values
        if code in EDUCATION_ENTRY_KINDS
    )
    status_values = [
        value["status"] if value["status"] == "unknown" else "not_specified",
        "in_progress",
        "completed",
    ]
    status_options = "".join(
        f"<option value='{_safe_text(code)}'{' selected' if code == value['status'] else ''}>{_safe_text(_EDUCATION_STATUS_LABELS.get(code, code.replace('_', ' ').title()))}</option>"
        for code in status_values
        if code in EDUCATION_ENTRY_STATUSES
    )
    year = "" if value["completion_year"] is None else str(value["completion_year"])
    new_attribute = " data-collection-new='true'" if template else ""
    return (
        f"<div class='review-collection-item education-entry{' is-removed' if removed else ''}' data-collection-item data-index='{index}'{new_attribute}>"
        f"<p class='fact-meta'>{_safe_text(source)}</p><div class='collection-item-controls education-entry-controls'>"
        f"<label class='review-field'><span>Education type</span><select id='{item_id}-kind' name='{prefix}kind'>{kind_options}</select></label>"
        f"<label class='review-field'><span>Qualification or course</span><input id='{item_id}-qualification' name='{prefix}qualification' value='{_safe_text(value['qualification'])}' maxlength='128'></label>"
        f"<label class='review-field'><span>Field of study</span><input id='{item_id}-field' name='{prefix}field' value='{_safe_text(value['field'])}' maxlength='128'></label>"
        f"<label class='review-field'><span>School or institution</span><input id='{item_id}-institution' name='{prefix}institution' value='{_safe_text(value['institution'])}' maxlength='128'></label>"
        f"<label class='review-field'><span>Status</span><select id='{item_id}-status' name='{prefix}status'>{status_options}</select></label>"
        f"<label class='review-field'><span>Completion year <small>(optional)</small></span><input id='{item_id}-completion-year' name='{prefix}completion_year' value='{_safe_text(year)}' inputmode='numeric' pattern='[0-9]{{4}}' maxlength='4'></label>"
        "</div>"
        f"<label class='collection-remove'><input type='checkbox' name='{prefix}remove' value='remove'{' checked' if removed else ''} data-collection-remove>"
        "<span class='remove-copy'>Remove</span><span class='restore-copy'>Keep entry</span></label></div>"
    )


def _render_review_collection_item(
    collection_id,
    spec,
    index,
    entry,
    *,
    template=False,
):
    prefix = f"review_collection_{collection_id}_{index}_"
    item_id = f"review-collection-{collection_id}-{index}"
    removed = entry["decision"] == "remove"
    source = (
        "Added by you"
        if entry["origin"] == "user"
        else _collection_source_label(entry["source_attributions"])
    )
    if spec["kind"] == "language":
        value = entry["value"]
        if type(value) is not LanguageValue:
            raise ProfileIntakeError("invalid_review_submission")
        current_proficiency = value.proficiency or ""
        options = [
            f"<option value=''{' selected' if not current_proficiency else ''}>Not specified</option>"
        ]
        if current_proficiency in {"unknown", "unspecified"}:
            options.append(
                f"<option value='{_safe_text(current_proficiency)}' selected>Not specified</option>"
            )
        for proficiency in sorted(
            LANGUAGE_PROFICIENCIES - {"unknown", "unspecified"}
        ):
            label = proficiency.replace("_", " ").title()
            options.append(
                f"<option value='{_safe_text(proficiency)}'{' selected' if proficiency == current_proficiency else ''}>{_safe_text(label)}</option>"
            )
        controls = (
            f"<label class='review-field'><span>Language</span><input id='{item_id}-language' name='{prefix}language' value='{_safe_text(value.language)}' maxlength='512' required></label>"
            f"<label class='review-field'><span>Proficiency</span><select id='{item_id}-proficiency' name='{prefix}proficiency'>{''.join(options)}</select></label>"
            f"<label class='review-field'><span>Locale or variety <small>(optional)</small></span><input id='{item_id}-locale' name='{prefix}locale' value='{_safe_text(value.locale or '')}' maxlength='512'></label>"
        )
    else:
        label = "Skill" if collection_id == "skills" else "Job title"
        token_class = " skill-token-field" if collection_id == "skills" else ""
        label_class = " class='screen-reader-only'" if collection_id == "skills" else ""
        size = (
            min(34, max(18, len(entry["value"]) + 1))
            if collection_id == "skills"
            else min(32, max(8, len(entry["value"])))
        )
        controls = (
            f"<label class='review-field{token_class}'><span{label_class}>{label}</span>"
            f"<input id='{item_id}-value' name='{prefix}value' value='{_safe_text(entry['value'])}' "
            f"maxlength='512' size='{size}' aria-label='{_safe_text(label)}' required></label>"
        )
    new_attribute = " data-collection-new='true'" if template else ""
    token_item_class = " skill-token" if collection_id == "skills" else ""
    remove_label = (
        " aria-label='Remove this skill'" if collection_id == "skills" else ""
    )
    remove_symbol = (
        "<span class='collection-remove-symbol' aria-hidden='true'>×</span>"
        if collection_id == "skills"
        else ""
    )
    return (
        f"<div class='review-collection-item{token_item_class}{' is-removed' if removed else ''}' data-collection-item data-index='{index}'{new_attribute}>"
        f"<p class='fact-meta'>{_safe_text(source)}</p><div class='collection-item-controls'>{controls}</div>"
        f"<label class='collection-remove'{remove_label}><input type='checkbox' name='{prefix}remove' value='remove'{' checked' if removed else ''} data-collection-remove>"
        f"{remove_symbol}<span class='remove-copy'>Remove</span><span class='restore-copy'>Keep item</span></label></div>"
    )


def _collection_source_label(attributions):
    kinds = {item.document_kind for item in attributions}
    if kinds == {DocumentKind.RESUME, DocumentKind.LINKEDIN_PROFILE_EXPORT}:
        return "Found in both"
    if kinds == {DocumentKind.RESUME}:
        return "Found in your resume"
    if kinds == {DocumentKind.LINKEDIN_PROFILE_EXPORT}:
        return "Found in your LinkedIn profile"
    raise ProfileIntakeError("invalid_review_submission")


def _review_fact_value_control(index, fact, raw_value, label):
    spec = _FIELD_SPECS.get(fact.field_path)
    if spec is None or spec.kind != "enum" or spec.multiple:
        return (
            f"<label class='review-field'>{_safe_text(label)}"
            f"<input name='fact_{index}_value' value='{_safe_text(raw_value)}' maxlength='512'></label>"
        )

    compact_suggestion = _uses_compact_suggestion_choice(fact)
    if fact.field_path == "experience.seniority":
        choices = list(candidate_seniority_display_choices(raw_value))
        primary = [choice for choice in choices if choice["primary"]]
        suggested = next(
            (choice for choice in choices if raw_value in choice["values"]), None
        )
        if compact_suggestion and suggested is not None and suggested not in primary:
            primary = [suggested, *primary]
        secondary = [choice for choice in choices if choice not in primary]
    else:
        primary_values = [
            option
            for option in _PRIMARY_CLASSIFICATION_CHOICES.get(fact.field_path, ())
            if option in spec.allowed
        ]
        if not primary_values:
            primary_values = sorted(spec.allowed)[:7]
        if compact_suggestion and raw_value in spec.allowed:
            primary_values = [
                raw_value, *(option for option in primary_values if option != raw_value)
            ]
        secondary_values = sorted(set(spec.allowed) - set(primary_values))
        insufficient_choice = None
        if _INSUFFICIENT_CLASSIFICATION_VALUES <= spec.allowed:
            insufficient_in_primary = bool(
                _INSUFFICIENT_CLASSIFICATION_VALUES.intersection(primary_values)
            )
            primary_values = [
                option
                for option in primary_values
                if option not in _INSUFFICIENT_CLASSIFICATION_VALUES
            ]
            secondary_values = [
                option
                for option in secondary_values
                if option not in _INSUFFICIENT_CLASSIFICATION_VALUES
            ]
            insufficient_choice = {
                "value": (
                    raw_value
                    if raw_value in _INSUFFICIENT_CLASSIFICATION_VALUES
                    else "not_specified"
                ),
                "values": tuple(sorted(_INSUFFICIENT_CLASSIFICATION_VALUES)),
                "label": "Not enough information",
                "description": (
                    "Your documents do not support a more specific choice."
                ),
                "primary": insufficient_in_primary,
            }
        primary = [
            {
                "value": option,
                "values": (option,),
                "label": option.replace("_", " ").title(),
                "description": _CLASSIFICATION_DESCRIPTIONS.get(
                    option,
                    f"Use the {option.replace('_', ' ').title()} classification.",
                ),
            }
            for option in primary_values
        ]
        secondary = [
            {
                "value": option,
                "values": (option,),
                "label": option.replace("_", " ").title(),
                "description": _CLASSIFICATION_DESCRIPTIONS.get(
                    option,
                    f"Use the {option.replace('_', ' ').title()} classification.",
                ),
            }
            for option in secondary_values
        ]
        if insufficient_choice is not None:
            target = primary if insufficient_choice.pop("primary") else secondary
            target.append(insufficient_choice)

    def option_markup(choice):
        option = choice["value"]
        option_slug = re.sub(r"[^a-z0-9]+", "-", option).strip("-")
        option_id = f"fact-{index}-{option_slug}"
        option_label = choice["label"]
        description = choice["description"]
        is_suggested = raw_value in choice["values"]
        is_checked = is_suggested and (
            not compact_suggestion or fact.decision == "accept"
        )
        suggestion_badge = (
            "<span class='suggestion-tag'>Suggested</span>"
            if compact_suggestion and is_suggested
            else ""
        )
        return (
            f"<label class='choice-card' for='{_safe_text(option_id)}'>"
            f"<input id='{_safe_text(option_id)}' type='radio' name='fact_{index}_value' "
            f"value='{_safe_text(option)}'{' checked' if is_checked else ''}{' required' if compact_suggestion else ''}>"
            f"<span><strong>{_safe_text(option_label)}</strong>{suggestion_badge}<small>{_safe_text(description)}</small></span></label>"
        )

    primary_choices = "".join(option_markup(choice) for choice in primary)
    secondary_choices = "".join(option_markup(choice) for choice in secondary)
    skip_choice = ""
    if compact_suggestion:
        skip_choice = (
            f"<label class='choice-card classification-skip' for='fact-{index}-leave-out'>"
            f"<input id='fact-{index}-leave-out' type='radio' name='fact_{index}_value' "
            f"value='{_SKIP_SUGGESTION_VALUE}'{' checked' if fact.decision == 'reject' else ''} required>"
            "<span><strong>Leave this suggestion out</strong><small>Do not add a classification for this item.</small></span></label>"
        )
    more_choices = (
        "<details class='choice-more classification-more'><summary>More classifications "
        f"<span>({len(secondary)})</span></summary><div class='choice-grid'>{secondary_choices}</div></details>"
        if secondary
        else ""
    )
    instruction = (
        "Choose one to add it to your profile, or leave the suggestion out."
        if compact_suggestion
        else "Choose the classification that fits best."
    )
    return (
        f"<fieldset class='choice-fieldset classification-fieldset'><legend>{_safe_text(label)}</legend>"
        f"<p class='muted classification-help'>{_safe_text(instruction)}</p>"
        f"<div class='choice-grid'>{primary_choices}{skip_choice}</div>"
        f"{more_choices}</fieldset>"
    )


def _render_preference_controls(model):
    canonical = preference_model_for_v2_editor(model)
    catalog = profile_preference_control_catalog_v2()
    immediate_sections = []
    secondary_sections = []
    secondary_selected_count = 0
    for dimension in catalog["dimensions"]:
        parent = canonical
        for part in dimension["path"]:
            parent = parent[part]
        selected = set(parent)
        choice_by_code = {choice["code"]: choice for choice in dimension["choices"]}
        ordered_choices = tuple(dimension["choices"])
        visible_choices = ordered_choices
        more_choices = ()
        if dimension["id"] == "job_interests":
            visible_codes = [
                code for code in _COMMON_JOB_INTEREST_CODES if code in choice_by_code
            ]
            visible_codes.extend(
                choice["code"]
                for choice in ordered_choices
                if choice["code"] in selected and choice["code"] not in visible_codes
            )
            visible_choices = tuple(choice_by_code[code] for code in visible_codes)
            visible_code_set = set(visible_codes)
            more_choices = tuple(
                choice
                for choice in ordered_choices
                if choice["code"] not in visible_code_set
            )

        def choice_markup(choice):
            field_name = _preference_choice_field(
                dimension["path"], choice["code"]
            )
            choice_id = field_name.replace("_", "-")
            return (
                f"<label class='choice-card' for='{choice_id}'>"
                f"<input id='{choice_id}' type='checkbox' name='{field_name}' value='selected'"
                f"{' checked' if choice['code'] in selected else ''}>"
                f"<span>{_safe_text(choice['label'])}</span></label>"
            )

        definitions = []
        for choice in ordered_choices:
            definitions.append(
                f"<div><dt>{_safe_text(choice['label'])}</dt>"
                f"<dd>{_safe_text(choice['description'])}</dd></div>"
            )
        help_id = (
            "preference-help-"
            + dimension["id"].replace(".", "-").replace("_", "-")
        )
        selected_labels = [
            choice["label"] for choice in ordered_choices if choice["code"] in selected
        ]
        compact_selected_labels = selected_labels[:4]
        if len(selected_labels) > 4:
            compact_selected_labels.append(f"+{len(selected_labels) - 4} more")
        selection_summary = (
            "<p class='selection-summary'><strong>Selected:</strong> "
            f"{_safe_text(', '.join(compact_selected_labels))}</p>"
            if selected_labels
            else ""
        )
        more_markup = (
            "<details class='choice-more job-interest-more'><summary>More job areas "
            f"<span>({len(more_choices)})</span>"
            "<span class='choice-more-selected'>Selections made</span></summary>"
            f"<div class='choice-grid'>{''.join(choice_markup(choice) for choice in more_choices)}</div></details>"
            if more_choices
            else ""
        )
        section = (
            f"<fieldset class='preference-group{' job-interest-group' if dimension['id'] == 'job_interests' else ''}' aria-describedby='{help_id}'>"
            f"<legend>{_safe_text(dimension['title'])}</legend><span class='selection-hint'>Choose all that apply</span>"
            f"<p class='muted' id='{help_id}'>{_safe_text(dimension['help'])}</p>"
            f"{selection_summary}<div class='choice-grid'>{''.join(choice_markup(choice) for choice in visible_choices)}</div>"
            f"{more_markup}<details class='choice-definitions-disclosure'><summary>What do these choices mean?</summary>"
            f"<dl class='choice-definitions'>{''.join(definitions)}</dl></details></fieldset>"
        )
        if dimension["id"] in _IMMEDIATE_PREFERENCE_DIMENSIONS:
            immediate_sections.append(section)
        else:
            secondary_sections.append(section)
            secondary_selected_count += len(selected)

    expectations = canonical["compensation_expectations"]
    expectation_items = "".join(
        _render_compensation_expectation(index, expectation, catalog)
        for index, expectation in enumerate(expectations)
    )
    expectation_template = _render_compensation_expectation(
        "__INDEX__",
        {
            "minimum_kind": "preferred",
            "amount": "",
            "currency": "USD",
            "period": "hour",
        },
        catalog,
        template=True,
    )
    compensation_section = (
        f"<fieldset class='preference-group compensation-group review-collection' data-review-collection='compensation' data-next-index='{len(expectations)}' data-limit='{MAX_COMPENSATION_EXPECTATIONS}'>"
        "<legend>Expected compensation</legend>"
        "<p class='muted'>Add each minimum you use, such as an hourly rate and a yearly salary. We never convert between currencies or time periods.</p>"
        "<dl class='compensation-kind-guide'><div><dt>Preferred minimum</dt><dd>Your target. You may choose to lower it later to see more opportunities.</dd></div><div><dt>Strict minimum</dt><dd>Your firm floor. Wahojobs will not suggest lowering it.</dd></div></dl>"
        f"<input type='hidden' name='{_PREFERENCE_COMPENSATION_MARKER}' value='present'>"
        f"<p class='collection-empty'{' hidden' if expectations else ''}>No minimum added. Jobs will not be limited by pay.</p>"
        f"<div class='review-collection-items compensation-expectation-items' data-collection-items>{expectation_items}</div>"
        "<button class='button-quiet collection-add' type='button' data-collection-add>Add another compensation expectation</button>"
        f"<template data-collection-template>{expectation_template}</template></fieldset>"
    )
    secondary_disclosure = (
        f"<details class='preference-disclosure more-preference-disclosure'{' open' if secondary_selected_count else ''}>"
        "<summary><span>More work preferences"
        "<small class='disclosure-selection-state'>Selections made — open to review</small>"
        "<small class='disclosure-empty-state'>Schedule, contract length, phone or voice work, and career level</small>"
        "</span></summary>"
        "<div class='disclosure-body'><p class='preference-evidence-note'>These are preferences, not automatic exclusions when a job leaves details out. If a job doesn’t specify this, we’ll still keep it in your matches.</p>"
        f"{''.join(secondary_sections)}</div></details>"
    )
    return "".join((*immediate_sections, compensation_section, secondary_disclosure))


def _render_compensation_expectation(index, expectation, catalog, *, template=False):
    prefix = f"preference_compensation_{index}_"
    item_id = f"preference-compensation-{index}"
    kind_options = "".join(
        f"<option value='{choice['code']}'{' selected' if choice['code'] == expectation['minimum_kind'] else ''}>{_safe_text(choice['label'])}</option>"
        for choice in catalog["compensation"]["minimum_kinds"]
    )
    currencies = tuple(catalog["compensation"]["currencies"])
    common = tuple(currency for currency in _COMMON_CURRENCIES if currency in currencies)
    common_set = set(common)
    remaining = tuple(currency for currency in currencies if currency not in common_set)

    def currency_options(values):
        return "".join(
            f"<option value='{currency}'{' selected' if currency == expectation['currency'] else ''}>{currency}</option>"
            for currency in values
        )

    currency_control = (
        f"<optgroup label='Common currencies'>{currency_options(common)}</optgroup>"
        f"<optgroup label='All other currencies'>{currency_options(remaining)}</optgroup>"
    )
    period_options = "".join(
        f"<option value='{period['code']}'{' selected' if period['code'] == expectation['period'] else ''}>{_safe_text(period['label'])}</option>"
        for period in catalog["compensation"]["periods"]
    )
    new_attribute = " data-collection-new='true'" if template else ""
    return (
        f"<div class='review-collection-item compensation-expectation' data-collection-item data-index='{index}'{new_attribute}>"
        "<div class='collection-item-controls compensation-expectation-controls'>"
        f"<label class='review-field'><span>Minimum type</span><select id='{item_id}-kind' name='{prefix}minimum_kind'>{kind_options}</select><small>Preferred can be relaxed later; strict is your firm floor.</small></label>"
        f"<label class='review-field'><span>Amount</span><input id='{item_id}-amount' name='{prefix}amount' inputmode='decimal' pattern='[0-9]{{1,18}}(?:\\.[0-9]{{1,2}})?' value='{_safe_text(expectation['amount'])}' maxlength='21' required></label>"
        f"<label class='review-field'><span>Currency</span><select id='{item_id}-currency' name='{prefix}currency'>{currency_control}</select><small>Type letters to jump through the full list.</small></label>"
        f"<label class='review-field'><span>Period</span><select id='{item_id}-period' name='{prefix}period'>{period_options}</select></label>"
        "</div>"
        f"<label class='collection-remove'><input type='checkbox' name='{prefix}remove' value='remove' data-collection-remove>"
        "<span class='remove-copy'>Remove</span><span class='restore-copy'>Keep expectation</span></label></div>"
    )


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
        "ai_import_checkpoint_available": "checkpoint_available",
        "ai_import_checkpoint_expired": "expired_checkpoint",
        "ai_import_checkpoint_stale": "stale_review",
        "stale_review": "stale_review",
        "ai_import_review_invalid": "invalid_review",
        "ai_import_schema_unavailable": "durable_unavailable",
        "durable_intake_unavailable": "durable_unavailable",
    }.get(code, "extraction_unavailable")


def _preflight_error_code(state):
    return {
        "profile_exists": "existing_profile",
        "entitlement_consumed": "existing_profile",
        "entitlement_reserved": "import_reserved",
        "checkpoint_available": "import_reserved",
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
        "ai_import_checkpoint_expired": "expired_draft",
        "ai_import_checkpoint_available": "checkpoint_available",
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
    if code == "existing_profile":
        return _response(
            HTTPStatus.CONFLICT,
            _page(
                "Your profile is ready",
                _authenticated_navigation()
                + "<section class='empty'><p class='eyebrow'>Your Wahojobs profile</p>"
                "<h1>Your profile is already set up</h1>"
                "<p>Review your saved profile or continue to the matches chosen for you.</p>"
                "<p><a class='primary-link' href='/account/profile'>View my profile</a></p>"
                "<p><a class='secondary-link' href='/find-matches'>See my matches</a></p>"
                "</section>",
            ),
        )
    status, title, message = {
        "invalid_request": (400, "Upload request unavailable", "This request is not valid."),
        "malformed_upload": (400, "Upload could not be read", "Choose one valid PDF or DOCX and try again."),
        "invalid_review": (400, "Review could not be updated", "Check the values and try again."),
        "authentication_required": (401, "Authentication required", "Sign in to continue."),
        "csrf_denied": (403, "Request rejected", "Reload the page and try again."),
        "authorization_denied": (404, "Page not found", "This page is not available."),
        "not_found": (404, "Page not found", "This page is not available."),
        "invalid_draft": (400, "Draft request unavailable", "Start the import again."),
        "expired_draft": (410, "Review session closed", "Your saved progress is still available. Return to the profile builder to continue."),
        "expired_checkpoint": (410, "Saved progress expired", "This saved review has reached its 7-day limit. Start a new profile import when you are ready."),
        "stale_review": (409, "Newer progress is available", "This review was updated in another tab or session. Continue from the saved version before making more changes."),
        "unresolved_review": (409, "Review needs confirmation", "Resolve every suggestion or source disagreement before saving."),
        "import_reserved": (409, "Import already in progress", "Finish or cancel the current import before starting another."),
        "checkpoint_available": (409, "Saved progress is available", "Continue your saved review or explicitly discard it before starting another import."),
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

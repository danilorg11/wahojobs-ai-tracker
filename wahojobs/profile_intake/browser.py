"""Private browser boundary for one authenticated AI-assisted profile draft."""

from __future__ import annotations

import base64
from dataclasses import replace
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
    INTERNAL_INFERRED_CLASSIFICATION_FIELD_PATHS,
    LanguageValue,
    LANGUAGE_PROFICIENCIES,
    ProfileIntakeError,
    _FIELD_SPECS,
)
from wahojobs.profiles.countries import CANONICAL_COUNTRIES
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
    review_reset_section_available,
    review_value_for_form,
    update_editable_review,
    review_with_display_name_input,
    reviewed_display_name,
    valid_review_display_name,
    _parse_review_value,
    _review_reset_section_for_fact,
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
_REVIEW_CONFIRM_FIELD = "review_confirm_step"
_SECTION_RESET_FIELD = "reset_section"
_PROFILE_BASICS_REVIEW_STEP = "review-found"
_BACKGROUND_REVIEW_STEP = "review-suggestions"
_HIDDEN_BACKGROUND_FIELD_PATHS = frozenset(
    {"experience.industries", "experience.seniority"}
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

_REVIEW_FIELD_LABELS = {
    "display_name": "Name",
    "country": "Based in",
    "total_years": "Total years of professional experience",
    "seniority": "Overall career stage",
    "industries": "Industry",
    "specialties": "Relevant specialties",
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
_STEP_FOUR_MAX_JOB_TITLES = 2
_STEP_FOUR_MAX_LANGUAGES = 3
_STEP_FOUR_MAX_PREFERENCE_ROWS = 6
_STEP_FOUR_DETAIL_LIMIT = 120
_MISSING_USER_FIELD_COPY = {
    "work_authorization": (
        "Work permission (optional)",
        "For example, a work permit. Leave blank if you are unsure; this is separate from where you live.",
    ),
    "eligible_countries": (
        "Countries where you have permission to work (optional)",
        "Separate countries with commas. These are alternatives, not your current residence or countries every job must accept.",
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
_REVIEW_STATE_SCRIPT = r"""(function(){
var reviewSteps=__REVIEW_STEPS__;
var form=document.getElementById('profile-review-form');
var autosave=document.getElementById('profile-review-autosave');
var renew=document.getElementById('profile-review-renewal');
var discard=document.getElementById('profile-review-discard');
var status=document.getElementById('profile-review-save-status');
var label=document.getElementById('profile-review-save-label');
var retry=document.getElementById('profile-review-save-retry');
var resume=document.getElementById('profile-review-save-resume');
if(!form||!renew||!status||!label||!window.fetch){return;}
var dirty=false;var saving=false;var current=null;var timer=null;
var invalidNameValue=null;var allowSubmit=false;var submitting=false;var allowDiscard=false;var lastActivity=0;var lastRenewed=Date.now();
var step=form.querySelector('input[name=review_step]');
var reviewConfirm=form.querySelector('input[name=review_confirm_step]');
var profileBasicsContinue=form.querySelector('[data-confirm-profile-basics]');
var backgroundContinue=form.querySelector('[data-confirm-background]');
var expertiseEditor=form.querySelector('[data-review-collection="skills"]');
var expertiseUndo=expertiseEditor&&expertiseEditor.querySelector('[data-expertise-undo]');
var expertiseUndoMessage=expertiseUndo&&expertiseUndo.querySelector('[data-expertise-undo-message]');
var sectionResetField=form.querySelector('[data-section-reset-field]');
var lastExpertiseUndo=null;
var workHistoryEditor=form.querySelector('[data-review-collection="job_titles"]');
var workHistoryUndo=workHistoryEditor&&workHistoryEditor.querySelector('[data-work-history-undo]');
var workHistoryUndoMessage=workHistoryUndo&&workHistoryUndo.querySelector('[data-work-history-undo-message]');
var lastWorkHistoryUndo=null;

function show(kind,text,canRetry,canResume){status.dataset.state=kind;label.textContent=text;if(retry){retry.hidden=!canRetry;}if(resume){resume.hidden=!canResume;}}
function activity(){lastActivity=Date.now();}
function updateForm(target,version,proof){if(!target){return false;}var versionInput=target.querySelector('input[name=version]');var proofInput=target.querySelector('input[name=csrf]');if(!versionInput||!proofInput||!/^[0-9]+$/.test(version)||!proof){return false;}versionInput.value=version;proofInput.value=proof;return true;}
function applyTokens(response){var version=response.headers.get('X-Wahojobs-Review-Version');return updateForm(form,version,response.headers.get('X-Wahojobs-CSRF-Save'))&&updateForm(autosave,version,response.headers.get('X-Wahojobs-CSRF-Autosave'))&&updateForm(renew,version,response.headers.get('X-Wahojobs-CSRF-Renew'))&&updateForm(discard,version,response.headers.get('X-Wahojobs-CSRF-Discard'));}
function setStep(value){if(!step||reviewSteps.indexOf(value)<0||step.value===value){return;}step.value=value;activity();schedule();}
function openStepFour(){setStep('review-finish');if(!autosave){window.location.hash='#review-finish';return;}window.clearTimeout(timer);flush().then(function(ok){if(ok){window.location.hash='#review-finish';window.location.reload();}});}
function rememberSection(event){var section=event.target.closest&&event.target.closest('section.review-section');if(section){setStep(section.id);}}

function replaceIndex(item,oldIndex,newIndex){
  Array.prototype.forEach.call(item.querySelectorAll('[name]'),function(control){control.name=control.name.replace('_'+oldIndex+'_','_'+newIndex+'_');});
  Array.prototype.forEach.call(item.querySelectorAll('[id]'),function(control){control.id=control.id.replace('-'+oldIndex+'-','-'+newIndex+'-');});
  Array.prototype.forEach.call(item.querySelectorAll('[for]'),function(control){control.htmlFor=control.htmlFor.replace('-'+oldIndex+'-','-'+newIndex+'-');});
  item.dataset.index=String(newIndex);
}
function renumberNewItems(editor){
  var items=editor.querySelectorAll('[data-collection-item]');var fresh=editor.querySelectorAll('[data-collection-new]');var next=items.length-fresh.length;
  Array.prototype.forEach.call(fresh,function(item){replaceIndex(item,item.dataset.index,next);next+=1;});editor.dataset.nextIndex=String(next);
  var empty=editor.querySelector('.collection-empty');if(empty){empty.hidden=items.length!==0;}
}
function collectionId(item){var editor=item.closest('[data-review-collection]');return editor?editor.dataset.reviewCollection:'';}
function updateCollectionEmpty(editor){if(!editor){return;}var empty=editor.querySelector('.collection-empty');if(empty){empty.hidden=!!editor.querySelector('[data-collection-item]:not([hidden])');}}
function expertiseItemValue(item){var input=item.querySelector('[name$="_value"]');var text=item.querySelector('.compact-expertise-text');return String(input?input.value:(text?text.textContent:'' )).trim();}
function clearExpertiseUndo(){if(lastExpertiseUndo&&lastExpertiseUndo.unsaved&&lastExpertiseUndo.item&&lastExpertiseUndo.item.hidden){var editor=lastExpertiseUndo.item.closest('[data-review-collection]');lastExpertiseUndo.item.remove();renumberNewItems(editor);updateCollectionEmpty(editor);}lastExpertiseUndo=null;if(expertiseUndo){expertiseUndo.hidden=true;}if(expertiseUndoMessage){expertiseUndoMessage.textContent='';}}
function offerExpertiseUndo(item,unsaved){clearExpertiseUndo();var value=expertiseItemValue(item)||'Expertise';lastExpertiseUndo={item:item,unsaved:!!unsaved};if(unsaved){Array.prototype.forEach.call(item.querySelectorAll('[name]'),function(control){control.disabled=true;});}item.hidden=true;item.setAttribute('aria-hidden','true');updateCollectionEmpty(expertiseEditor);if(expertiseUndoMessage){expertiseUndoMessage.textContent=value+' removed.';}if(expertiseUndo){expertiseUndo.hidden=false;var button=expertiseUndo.querySelector('[data-expertise-undo-action]');if(button){button.focus();}}}
function undoExpertise(){if(!lastExpertiseUndo||!lastExpertiseUndo.item){return;}var item=lastExpertiseUndo.item;var unsaved=lastExpertiseUndo.unsaved;lastExpertiseUndo=null;Array.prototype.forEach.call(item.querySelectorAll('[name]'),function(control){control.disabled=false;});var remove=item.querySelector('[data-collection-remove]');if(remove){remove.checked=false;}item.hidden=false;item.removeAttribute('aria-hidden');if(expertiseUndo){expertiseUndo.hidden=true;}if(expertiseUndoMessage){expertiseUndoMessage.textContent='';}updateCollectionEmpty(expertiseEditor);activity();dirty=true;window.clearTimeout(timer);flush().then(function(ok){if(ok){var focus=item.querySelector('input:not([type=checkbox]),select');if(focus){focus.focus();}}else if(unsaved){offerExpertiseUndo(item,true);}});}
function clearWorkHistoryUndo(){if(lastWorkHistoryUndo&&lastWorkHistoryUndo.unsaved&&lastWorkHistoryUndo.item&&lastWorkHistoryUndo.item.hidden){var editor=lastWorkHistoryUndo.item.closest('[data-review-collection]');lastWorkHistoryUndo.item.remove();renumberNewItems(editor);updateCollectionEmpty(editor);}lastWorkHistoryUndo=null;if(workHistoryUndo){workHistoryUndo.hidden=true;}if(workHistoryUndoMessage){workHistoryUndoMessage.textContent='';}}
function offerWorkHistoryUndo(item,unsaved){clearWorkHistoryUndo();var value=expertiseItemValue(item)||'Job title';lastWorkHistoryUndo={item:item,unsaved:!!unsaved};if(unsaved){Array.prototype.forEach.call(item.querySelectorAll('[name]'),function(control){control.disabled=true;});}item.hidden=true;item.setAttribute('aria-hidden','true');updateCollectionEmpty(workHistoryEditor);if(workHistoryUndoMessage){workHistoryUndoMessage.textContent=value+' removed.';}if(workHistoryUndo){workHistoryUndo.hidden=false;var button=workHistoryUndo.querySelector('[data-work-history-undo-action]');if(button){button.focus();}}}
function undoWorkHistory(){if(!lastWorkHistoryUndo||!lastWorkHistoryUndo.item){return;}var item=lastWorkHistoryUndo.item;var unsaved=lastWorkHistoryUndo.unsaved;lastWorkHistoryUndo=null;Array.prototype.forEach.call(item.querySelectorAll('[name]'),function(control){control.disabled=false;});var remove=item.querySelector('[data-collection-remove]');if(remove){remove.checked=false;}item.hidden=false;item.removeAttribute('aria-hidden');if(workHistoryUndo){workHistoryUndo.hidden=true;}if(workHistoryUndoMessage){workHistoryUndoMessage.textContent='';}updateCollectionEmpty(workHistoryEditor);activity();dirty=true;window.clearTimeout(timer);flush().then(function(ok){if(ok){var focus=item.querySelector('input:not([type=checkbox]),select');if(focus){focus.focus();}}else if(unsaved){offerWorkHistoryUndo(item,true);}});}
function clearSectionUndo(section){if(section==='expertise'){clearExpertiseUndo();}if(section==='work_history'){clearWorkHistoryUndo();}}
function closeSectionReset(panel,returnFocus){if(panel){panel.hidden=true;}if(returnFocus){returnFocus.setAttribute('aria-expanded','false');returnFocus.focus();}}
function removeCollectionItem(remove){var item=remove&&remove.closest('[data-collection-item]');var editor=item&&item.closest('[data-review-collection]');if(!item||!editor){return;}remove.checked=true;var kind=editor.dataset.reviewCollection;if(kind==='skills'){offerExpertiseUndo(item,item.hasAttribute('data-collection-new'));return;}if(kind==='job_titles'){offerWorkHistoryUndo(item,item.hasAttribute('data-collection-new'));return;}if(item.hasAttribute('data-collection-new')){item.remove();renumberNewItems(editor);updateCollectionEmpty(editor);return;}item.hidden=true;item.setAttribute('aria-hidden','true');updateCollectionEmpty(editor);}
function controlValue(item,suffix){var control=item.querySelector('[name$="_'+suffix+'"]');return control?String(control.value||'').trim():'';}
function firstControl(item,suffix){return item.querySelector('[name$="_'+suffix+'"]')||item.querySelector('input:not([type=checkbox]),select');}
function isRemoved(item){var remove=item.querySelector('[data-collection-remove]');return !!(remove&&remove.checked);}
function itemState(item,allowPending){
  var kind=collectionId(item);var empty=false;var message='';var control=firstControl(item,'value');
  if(isRemoved(item)){return {valid:true,empty:false,control:null,message:''};}
  if(kind==='skills'||kind==='job_titles'){
    empty=!controlValue(item,'value');control=firstControl(item,'value');
    if(!empty&&control&&!control.checkValidity()){message=kind==='skills'?'Enter a valid skill.':'Enter a valid job title.';}
    else if(empty&&!item.hasAttribute('data-collection-new')){message=kind==='skills'?'Enter a skill or remove this item.':'Enter a job title or remove this item.';}
  }else if(kind==='languages'){
    var language=controlValue(item,'language');var proficiency=controlValue(item,'proficiency');var locale=controlValue(item,'locale');empty=!language&&!proficiency&&!locale;control=firstControl(item,'language');
    if(!empty&&!language){message='Enter the language name before this item can be saved.';}
    else if(control&&!control.checkValidity()){message='Enter a valid language name.';}
  }else if(kind==='education'){
    var educationKind=controlValue(item,'kind');var qualification=controlValue(item,'qualification');var field=controlValue(item,'field');var institution=controlValue(item,'institution');var year=controlValue(item,'completion_year');var educationStatus=controlValue(item,'status');
    empty=(educationKind==='not_specified'||!educationKind)&&!qualification&&!field&&!institution&&!year&&(educationStatus==='not_specified'||educationStatus==='unknown'||!educationStatus);
    control=firstControl(item,'qualification');
    if(!empty&&(educationKind==='not_specified'||!educationKind)&&!qualification&&!field&&!institution){message='Add an education type, qualification, field of study, or school.';}
    else if(year&&(!/^[0-9]{4}$/.test(year)||Number(year)<1900||Number(year)>2200)){control=firstControl(item,'completion_year');message='Use a four-digit completion year between 1900 and 2200.';}
  }else if(kind==='compensation'){
    var amount=controlValue(item,'amount');empty=!amount;control=firstControl(item,'amount');
    if(!empty&&(!/^[0-9]{1,18}(?:\.[0-9]{1,2})?$/.test(amount)||Number(amount)<=0)){message='Enter a positive amount with no more than two decimal places.';}
  }
  if(!message&&!allowPending&&item.hasAttribute('data-decision-required')&&!item.querySelector('input[name$="_decision"]:checked')){control=item.querySelector('input[name$="_decision"]');message='Choose Keep or Remove before finding matches.';}
  if(item.hasAttribute('data-collection-new')&&empty){return {valid:true,empty:true,control:control,message:''};}
  if(!message){
    var invalid=Array.prototype.find.call(item.querySelectorAll('input:not([type=checkbox]),select'),function(candidate){return !candidate.checkValidity();});
    if(invalid){control=invalid;message='Finish this item before it can be saved.';}
  }
  return {valid:!message,empty:empty,control:control,message:message};
}
function clearItemError(item){
  item.classList.remove('collection-item-needs-attention');var error=item.querySelector('[data-collection-error]');
  Array.prototype.forEach.call(item.querySelectorAll('[aria-invalid="true"]'),function(control){control.removeAttribute('aria-invalid');if(error){var described=(control.getAttribute('aria-describedby')||'').split(/\s+/).filter(function(id){return id&&id!==error.id;});if(described.length){control.setAttribute('aria-describedby',described.join(' '));}else{control.removeAttribute('aria-describedby');}}});
  if(error){error.hidden=true;error.textContent='';}
}
function showItemError(item,state){
  clearItemError(item);item.classList.add('collection-item-needs-attention');var error=item.querySelector('[data-collection-error]');var control=state.control||item.querySelector('input:not([type=checkbox]),select');
  if(error){error.textContent=state.message;error.hidden=false;}
  if(control){control.setAttribute('aria-invalid','true');if(error&&error.id){var described=(control.getAttribute('aria-describedby')||'').split(/\s+/).filter(Boolean);if(described.indexOf(error.id)<0){described.push(error.id);}control.setAttribute('aria-describedby',described.join(' '));}}
}
function resetOwnsCollection(item){if(!sectionResetField||sectionResetField.disabled){return false;}var owned={work_history:'job_titles',education:'education',languages:'languages',expertise:'skills'};return collectionId(item)===owned[sectionResetField.value];}
function updateStepAttention(){
  Array.prototype.forEach.call(document.querySelectorAll('section.review-section'),function(section){var needs=!!section.querySelector('.collection-item-needs-attention');section.classList.toggle('review-step-needs-attention',needs);var link=document.querySelector('.review-progress a[href="#'+section.id+'"]');if(link){link.classList.toggle('needs-attention',needs);var note=link.querySelector('[data-step-attention]');if(note){note.hidden=!needs;}}});
}
function validateCollections(focusFirst,allowPending){
  var first=null;Array.prototype.forEach.call(form.querySelectorAll('[data-collection-item]'),function(item){if(resetOwnsCollection(item)){clearItemError(item);return;}var state=itemState(item,!!allowPending);if(state.valid){clearItemError(item);}else{showItemError(item,state);if(!first){first=state.control||item;}}});updateStepAttention();
  if(first){show('attention','Finish the highlighted item before it can be saved.',false,false);if(focusFirst){first.focus();if(first.scrollIntoView){first.scrollIntoView({behavior:'smooth',block:'center'});}}return false;}return true;
}
function autosaveMaterial(){
  var disabled=[];var submittedNew=[];Array.prototype.forEach.call(form.querySelectorAll('[data-collection-new]'),function(item){var state=itemState(item,true);if(state.empty){Array.prototype.forEach.call(item.querySelectorAll('[name]'),function(control){disabled.push([control,control.disabled]);control.disabled=true;});}else{submittedNew.push(item);}});
  var data=new URLSearchParams(new FormData(form));Array.prototype.forEach.call(disabled,function(entry){entry[0].disabled=entry[1];});return {data:data,submittedNew:submittedNew};
}
function dropEmptyPlaceholders(){
  var editors=[];Array.prototype.forEach.call(form.querySelectorAll('[data-collection-new]'),function(item){if(itemState(item).empty){var editor=item.closest('[data-review-collection]');if(editor&&editors.indexOf(editor)<0){editors.push(editor);}item.remove();}});Array.prototype.forEach.call(editors,renumberNewItems);
}
function updatePreferenceSummaries(){
  Array.prototype.forEach.call(form.querySelectorAll('[data-preference-dimension]'),function(group){var summary=group.querySelector('[data-selection-summary]');if(!summary){return;}var labels=[];Array.prototype.forEach.call(group.querySelectorAll('input[type=checkbox]:checked'),function(input){var strong=input.closest('label')&&input.closest('label').querySelector('strong');if(strong){labels.push(strong.textContent.trim());}});var visible=labels.slice(0,4);if(labels.length>4){visible.push('+'+(labels.length-4)+' more');}var values=summary.querySelector('[data-selection-values]');if(values){values.textContent=visible.join(', ');}summary.hidden=!labels.length;});
  var secondary=form.querySelector('.more-preference-disclosure');if(secondary){var count=secondary.querySelectorAll('input[type=checkbox]:checked').length;var state=secondary.querySelector('.disclosure-selection-state');if(state){state.textContent=count+(count===1?' selection':' selections')+' — open to review';}}
}
function updatePreferenceMode(group,reset){
  var mode=group.querySelector('[data-preference-mode]:checked');var panel=group.querySelector('[data-preference-options]');var error=group.querySelector('[data-preference-mode-error]');var active=!!(mode&&mode.value==='preferences');
  if(!active&&reset){Array.prototype.forEach.call(group.querySelectorAll('[data-preference-options] input[type=checkbox]'),function(input){input.checked=false;});}
  Array.prototype.forEach.call(group.querySelectorAll('[data-preference-options] input[type=checkbox]'),function(input){input.disabled=!active;});
  if(panel){panel.hidden=!active;}
  var preferences=group.querySelector('[data-preference-mode][value="preferences"]');if(preferences){preferences.setAttribute('aria-expanded',active?'true':'false');}
  var valid=!active||!!group.querySelector('[data-preference-options] input[type=checkbox]:checked');if(valid&&error){error.hidden=true;}if(valid&&preferences){preferences.removeAttribute('aria-invalid');preferences.removeAttribute('aria-describedby');}
}
function updatePreferenceModes(target){Array.prototype.forEach.call(form.querySelectorAll('[data-preference-dimension]'),function(group){var reset=!!(target&&target.matches&&target.matches('[data-preference-mode]')&&target.value==='unrestricted'&&group.contains(target));updatePreferenceMode(group,reset);});}
function validatePreferenceModes(focusFirst,showErrors){
  var first=null;Array.prototype.forEach.call(form.querySelectorAll('[data-preference-dimension]'),function(group){var mode=group.querySelector('[data-preference-mode]:checked');var preferences=group.querySelector('[data-preference-mode][value="preferences"]');var error=group.querySelector('[data-preference-mode-error]');var valid=!!mode&&(mode.value!=='preferences'||!!group.querySelector('[data-preference-options] input[type=checkbox]:checked'));if(valid){if(error){error.hidden=true;}if(preferences){preferences.removeAttribute('aria-invalid');preferences.removeAttribute('aria-describedby');}}else if(showErrors){if(error){error.hidden=false;}if(preferences){preferences.setAttribute('aria-invalid','true');if(error&&error.id){preferences.setAttribute('aria-describedby',error.id);}}if(!first){first=preferences||mode||group;}}});
  if(first){show('attention','Choose at least one option, or select No preference.',false,false);if(focusFirst){first.focus();if(first.scrollIntoView){first.scrollIntoView({behavior:'smooth',block:'center'});}}return false;}return !Array.prototype.some.call(form.querySelectorAll('[data-preference-dimension]'),function(group){var mode=group.querySelector('[data-preference-mode]:checked');return !mode||(mode.value==='preferences'&&!group.querySelector('[data-preference-options] input[type=checkbox]:checked'));});
}
function schedule(){if(!autosave){return;}dirty=true;window.clearTimeout(timer);timer=window.setTimeout(function(){saveNow(false);},1500);}
function saveNow(keepalive){
  if(!autosave||!dirty){return current||Promise.resolve(true);}if(saving){return current;}if(!validateCollections(false,true)||!validatePreferenceModes(false,false)){dirty=true;return Promise.resolve(false);}
  saving=true;dirty=false;show('saving','Saving…',false,false);var material=autosaveMaterial();var data=material.data;data.set('action','autosave');data.set('version',autosave.querySelector('input[name=version]').value);data.set('csrf',autosave.querySelector('input[name=csrf]').value);
  current=window.fetch(form.getAttribute('action'),{method:'POST',body:data.toString(),credentials:'same-origin',keepalive:!!keepalive,headers:{'Content-Type':'application/x-www-form-urlencoded'}}).then(function(response){if(response.status===204&&applyTokens(response)){Array.prototype.forEach.call(material.submittedNew,function(item){item.removeAttribute('data-collection-new');});var invalidField=response.headers.get('X-Wahojobs-Review-Invalid-Field');if(invalidField){invalidNameValue=data.get(invalidField);showDisplayNameError();if(submitting){focusDisplayName();}return false;}if(invalidNameValue!==null){invalidNameValue=null;validateDisplayName();}show('saved','Progress saved',false,false);return true;}dirty=true;if(response.status===409){show('conflict','Newer progress was saved in another tab.',false,true);}else if(response.status===410){show('expired','Your active review closed. Continue from your saved progress.',false,true);}else if(response.status===400){show('error','Check the highlighted details, then retry saving.',true,false);}else{show('error','We could not save your progress. Try again.',true,false);}return false;}).catch(function(){dirty=true;show('error','We could not save your progress. Try again.',true,false);return false;}).finally(function(){saving=false;current=null;});return current;
}
function flush(){return saveNow(false).then(function(ok){return ok&&dirty?flush():ok;});}
function profileBasicsNeedsConfirmation(){return !!form.querySelector('#review-found [data-profile-basics-pending]');}
function unresolvedProfileBasicsChoice(){return Array.prototype.find.call(form.querySelectorAll('#review-found [data-profile-basics-pending] select[name$="_decision"]'),function(control){return control.value==='pending';});}
function clearProfileBasicsPending(){Array.prototype.forEach.call(form.querySelectorAll('#review-found [data-profile-basics-pending]'),function(item){var control=item.querySelector('select[name$="_decision"]');if(!control||control.value!=='pending'){item.removeAttribute('data-profile-basics-pending');}});}
function backgroundNeedsConfirmation(){return !!form.querySelector('#review-suggestions [data-background-pending]');}
function clearBackgroundPending(){Array.prototype.forEach.call(form.querySelectorAll('#review-suggestions [data-background-pending]'),function(item){item.removeAttribute('data-background-pending');});}
function confirmReviewStep(source,target){
  if(!reviewConfirm||reviewSteps.indexOf(target)<0||!validateCollections(true,true)){return;}
  if(source==='review-found'){
    var unresolved=unresolvedProfileBasicsChoice();if(unresolved){show('attention','Choose which document detail is correct before continuing.',false,false);unresolved.focus();if(unresolved.scrollIntoView){unresolved.scrollIntoView({behavior:'smooth',block:'center'});}return;}
    var invalid=document.querySelector('#review-found :invalid');if(invalid){show('attention','Check the highlighted profile detail before continuing.',false,false);invalid.focus();if(invalid.scrollIntoView){invalid.scrollIntoView({behavior:'smooth',block:'center'});}return;}
  }
  if(source==='review-found'){clearWorkHistoryUndo();}
  if(source==='review-suggestions'){clearExpertiseUndo();}
  reviewConfirm.disabled=false;reviewConfirm.value=source;if(step){step.value=target;}activity();dirty=true;window.clearTimeout(timer);
  if(!autosave){window.location.hash='#'+target;return;}
  flush().then(function(ok){reviewConfirm.disabled=true;reviewConfirm.value='';if(ok){if(source==='review-found'){clearProfileBasicsPending();}else{clearBackgroundPending();}if(source==='review-found'&&profileBasicsNeedsConfirmation()){show('attention','Choose which document detail is correct before continuing.',false,false);return;}window.location.hash='#'+target;if(target==='review-finish'&&autosave){window.location.reload();return;}var heading=document.getElementById(target+'-title');if(heading){heading.focus({preventScroll:true});}}});
}
function requireReviewConfirmation(){if(profileBasicsNeedsConfirmation()){show('attention','Review your profile basics before finding matches.',false,false);if(profileBasicsContinue){profileBasicsContinue.focus();if(profileBasicsContinue.scrollIntoView){profileBasicsContinue.scrollIntoView({behavior:'smooth',block:'center'});}}return false;}if(backgroundNeedsConfirmation()){show('attention','Review your skills and experience before finding matches.',false,false);if(backgroundContinue){backgroundContinue.focus();if(backgroundContinue.scrollIntoView){backgroundContinue.scrollIntoView({behavior:'smooth',block:'center'});}}return false;}return true;}
function focusFirstNativeInvalid(){var first=form.querySelector(':invalid');if(!first){return false;}var section=first.closest('section.review-section');if(section){section.classList.add('review-step-needs-attention');var link=document.querySelector('.review-progress a[href="#'+section.id+'"]');if(link){link.classList.add('needs-attention');var note=link.querySelector('[data-step-attention]');if(note){note.hidden=false;}}}show('attention','Finish the highlighted detail before finding matches.',false,false);first.focus();if(first.scrollIntoView){first.scrollIntoView({behavior:'smooth',block:'center'});}return true;}
function focusDisplayName(){var field=form.querySelector('[data-display-name]');if(field){field.focus();field.scrollIntoView({block:'center'});}}
function showDisplayNameError(){var error=document.getElementById('display-name-error');if(error){error.hidden=false;}Array.prototype.forEach.call(form.querySelectorAll('[data-display-name]'),function(field){field.setAttribute('aria-invalid','true');});show('attention','Check your display name. Your other edits are saved.',false,false);}
function validateDisplayName(){var fields=form.querySelectorAll('[data-display-name]');var valid=Array.prototype.some.call(fields,function(field){return !!field.value.trim()&&field.value.trim().length<=160&&field.value!==invalidNameValue;});var error=document.getElementById('display-name-error');if(error){error.hidden=valid;}Array.prototype.forEach.call(fields,function(field){if(valid){field.removeAttribute('aria-invalid');}else{field.setAttribute('aria-invalid','true');}});if(!valid){show('attention','Enter the display name you would like us to use.',false,false);focusDisplayName();}return valid;}
var nameLink=form.querySelector('[data-focus-display-name]');if(nameLink){nameLink.addEventListener('click',function(event){event.preventDefault();focusDisplayName();});}
var nameError=document.getElementById('display-name-error');if(nameError&&!nameError.hidden){window.setTimeout(focusDisplayName,0);}
var fieldErrorLink=form.querySelector('[data-review-error-target]');
if(fieldErrorLink){var errorField=document.getElementById(fieldErrorLink.dataset.reviewErrorTarget);var fieldError=document.getElementById('profile-field-error');if(errorField&&fieldError){errorField.setAttribute('aria-invalid','true');errorField.setAttribute('aria-describedby','profile-field-error');var errorItem=errorField.closest('[data-collection-item]')||errorField.closest('label');if(errorItem){errorItem.insertAdjacentElement('afterend',fieldError);fieldError.style.flexBasis='100%';fieldError.style.minWidth='0';fieldError.style.maxWidth='100%';}function focusReviewError(){var parent=errorField.parentElement;while(parent&&parent!==form){if(parent.tagName==='DETAILS'){parent.open=true;}parent=parent.parentElement;}errorField.focus();errorField.scrollIntoView({block:'center'});}fieldErrorLink.addEventListener('click',function(event){event.preventDefault();focusReviewError();});window.setTimeout(focusReviewError,0);}}


Array.prototype.forEach.call(document.querySelectorAll('.review-progress a[href^="#review-"]'),function(link){link.addEventListener('click',function(event){var target=link.getAttribute('href').slice(1);if(step&&step.value==='review-found'&&target!=='review-found'&&profileBasicsNeedsConfirmation()){event.preventDefault();confirmReviewStep('review-found',target);return;}if(step&&step.value==='review-suggestions'&&['review-preferences','review-finish'].indexOf(target)>=0&&backgroundNeedsConfirmation()){event.preventDefault();confirmReviewStep('review-suggestions',target);return;}if(step&&step.value==='review-found'&&target!=='review-found'){clearWorkHistoryUndo();}if(step&&step.value==='review-suggestions'&&target!=='review-suggestions'){clearExpertiseUndo();}if(target==='review-finish'){event.preventDefault();openStepFour();return;}setStep(target);});});
form.addEventListener('focusin',rememberSection);form.addEventListener('pointerdown',rememberSection);window.addEventListener('hashchange',function(){setStep(window.location.hash.slice(1));});
form.addEventListener('click',function(event){
  var undo=event.target.closest&&event.target.closest('[data-expertise-undo-action]');if(undo){event.preventDefault();undoExpertise();return;}
  var workUndo=event.target.closest&&event.target.closest('[data-work-history-undo-action]');if(workUndo){event.preventDefault();undoWorkHistory();return;}
  var resetOpen=event.target.closest&&event.target.closest('[data-section-reset-open]');
  if(resetOpen){event.preventDefault();var panelId=resetOpen.getAttribute('aria-controls');var panel=panelId&&document.getElementById(panelId);resetOpen.setAttribute('aria-expanded','true');if(panel){panel.hidden=false;var cancel=panel.querySelector('[data-section-reset-cancel]');if(cancel){cancel.focus();}}return;}
  var resetCancel=event.target.closest&&event.target.closest('[data-section-reset-cancel]');
  if(resetCancel){event.preventDefault();var cancelPanel=resetCancel.closest('[data-section-reset-confirm]');var cancelOpen=cancelPanel&&form.querySelector('[aria-controls="'+cancelPanel.id+'"]');closeSectionReset(cancelPanel,cancelOpen);return;}
  var resetConfirm=event.target.closest&&event.target.closest('[data-section-reset-confirm-action]');
  if(resetConfirm){event.preventDefault();if(!sectionResetField){return;}var confirmPanel=resetConfirm.closest('[data-section-reset-confirm]');var confirmOpen=confirmPanel&&form.querySelector('[aria-controls="'+confirmPanel.id+'"]');var section=resetConfirm.getAttribute('data-section-reset-confirm-action');clearSectionUndo(section);sectionResetField.value=section;sectionResetField.disabled=false;if(confirmPanel){confirmPanel.hidden=true;}if(confirmOpen){confirmOpen.setAttribute('aria-expanded','false');}activity();dirty=true;window.clearTimeout(timer);flush().then(function(ok){if(ok){window.location.reload();}else{sectionResetField.disabled=true;sectionResetField.value='';}});return;}
  var removeAction=event.target.closest&&event.target.closest('[data-collection-remove-action]');if(removeAction){event.preventDefault();var remove=removeAction.closest('[data-collection-item]')&&removeAction.closest('[data-collection-item]').querySelector('[data-collection-remove]');removeCollectionItem(remove);activity();validateCollections(false,true);schedule();return;}
  var basics=event.target.closest&&event.target.closest('[data-confirm-profile-basics]');if(basics){event.preventDefault();confirmReviewStep('review-found','review-suggestions');return;}
  var confirm=event.target.closest&&event.target.closest('[data-confirm-background]');if(confirm){event.preventDefault();confirmReviewStep('review-suggestions','review-preferences');return;}
  var add=event.target.closest&&event.target.closest('[data-collection-add]');if(!add){return;}var editor=add.closest('[data-review-collection]');if(editor&&editor.dataset.reviewCollection==='skills'){clearExpertiseUndo();}if(editor&&editor.dataset.reviewCollection==='job_titles'){clearWorkHistoryUndo();}var template=editor&&editor.querySelector('template[data-collection-template]');var container=editor&&editor.querySelector('[data-collection-items]');var index=Number(editor&&editor.dataset.nextIndex);var limit=Number(editor&&editor.dataset.limit);if(!template||!container||!Number.isInteger(index)||index<0||index>=limit){return;}var fragment=template.content.cloneNode(true);var item=fragment.querySelector('[data-collection-item]');replaceIndex(item,'__INDEX__',index);container.appendChild(fragment);editor.dataset.nextIndex=String(index+1);updateCollectionEmpty(editor);var input=item.querySelector('input:not([type=checkbox]),select');if(input){input.focus();}activity();
});
form.addEventListener('change',function(event){if(event.target.matches&&event.target.matches('[data-collection-remove]')&&event.target.checked){removeCollectionItem(event.target);}else{if(expertiseEditor&&expertiseEditor.contains(event.target)){clearExpertiseUndo();}if(workHistoryEditor&&workHistoryEditor.contains(event.target)){clearWorkHistoryUndo();}}activity();updatePreferenceModes(event.target);updatePreferenceSummaries();validateCollections(false,true);schedule();});
form.addEventListener('input',function(event){if(expertiseEditor&&expertiseEditor.contains(event.target)){clearExpertiseUndo();}if(workHistoryEditor&&workHistoryEditor.contains(event.target)){clearWorkHistoryUndo();}activity();updatePreferenceSummaries();validateCollections(false,true);schedule();});
form.addEventListener('keydown',function(event){if(event.key==='Enter'&&event.target.matches&&event.target.matches('input:not([type=submit]):not([type=button])')&&event.target.closest('[data-collection-item]')){event.preventDefault();validateCollections(true,false);}});
form.addEventListener('submit',function(event){if(allowSubmit){return;}event.preventDefault();if(submitting){return;}var submitter=event.submitter||form.querySelector('button[type=submit]');if(!submitter){return;}if(!validateDisplayName()||!validateCollections(true,false)||!validatePreferenceModes(true,true)||!requireReviewConfirmation()){return;}clearWorkHistoryUndo();clearExpertiseUndo();dropEmptyPlaceholders();if(!form.checkValidity()){form.reportValidity();focusFirstNativeInvalid();return;}submitting=true;flush().then(function(ok){if(!ok){submitting=false;return;}window.setTimeout(function(){allowSubmit=true;form.setAttribute('aria-busy','true');show('saving','Creating your profile…',false,false);if(form.requestSubmit){form.requestSubmit(submitter);}else{form.submit();}allowSubmit=false;},0);}).catch(function(){submitting=false;show('error','We could not submit your profile. Your review is saved; please try again.',true,false);});});
if(discard){discard.addEventListener('submit',function(event){window.clearTimeout(timer);dirty=false;if(allowDiscard||!saving){return;}event.preventDefault();Promise.resolve(current).then(function(){dirty=false;allowDiscard=true;if(discard.requestSubmit){discard.requestSubmit();}else{discard.submit();}allowDiscard=false;});});}
if(retry){retry.addEventListener('click',function(){saveNow(false);});}
function tick(){var now=Date.now();if(saving||document.visibilityState!=='visible'||!lastActivity||now-lastActivity>360000||now-lastRenewed<300000){return;}lastRenewed=now;var data=new URLSearchParams(new FormData(renew));window.fetch(renew.getAttribute('action'),{method:'POST',body:data.toString(),credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded'}}).then(function(response){if(response.status===409){show('conflict','Newer progress was saved in another tab.',false,true);return;}if(response.status===410){show('expired','Your active review closed. Continue from your saved progress.',false,true);return;}if(!response.ok){return;}var remaining=Number(response.headers.get('X-Wahojobs-Review-Absolute-Seconds'));if(Number.isFinite(remaining)&&remaining<=600){show('warning','This review session closes in about '+Math.max(1,Math.ceil(remaining/60))+' minutes. Your saved progress will remain available.',false,false);}}).catch(function(){});}
window.setInterval(tick,60000);window.addEventListener('pagehide',function(){if(dirty&&!saving){saveNow(true);}});updatePreferenceModes(null);updatePreferenceSummaries();show('saved',autosave?'Progress saved':'Review active',false,false);if(window.location.hash==='#review-finish'){window.setTimeout(function(){var finish=document.getElementById('review-finish');if(finish){finish.scrollIntoView({block:'start'});}},0);}
}());""".replace(
    "__REVIEW_STEPS__",
    json.dumps(PROFILE_INTAKE_REVIEW_STEPS, ensure_ascii=True),
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
        allowed = {"draft", "check"} if method in {"GET", "HEAD"} else {"draft"}
        if (parsed.fragment or not {"draft"} <= set(parameters) <= allowed
                or len(parameters["draft"]) != 1
                or ("check" in parameters and parameters["check"] != ["1"])):
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
            issue = None
            if "check" in parameters:
                issue = self._processing.review_validation_issue(reference, grant)
            return _form_page_response(
                HTTPStatus.OK,
                _review_page(
                    reference,
                    snapshot,
                    csrf_secret,
                    save_enabled=self._processing.durable_save_enabled,
                    validation_issue=issue,
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
        # Reject unsafe name input without losing the rest of the review or
        # persisting contact details as a display name. Final validation below
        # returns the editable form with its field-specific error.
        invalid_name_field = None
        if action in {"save", "autosave"}:
            name_fields = [
                f"fact_{index}_value" for index, fact in enumerate(snapshot.review.facts)
                if fact.field_path == "identity.display_name"
            ] or ["missing_display_name"]
            form = dict(form)
            for name_field in name_fields:
                value = _single(form, name_field)
                if value is not None and not valid_review_display_name(value):
                    form[name_field] = [""]
                    if value.strip():
                        invalid_name_field = name_field
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
                ) + ((("X-Wahojobs-Review-Invalid-Field", invalid_name_field),)
                     if invalid_name_field else ()),
            )
        if action == "save":
            if not valid_review_display_name(reviewed_display_name(review)):
                try:
                    _state, updated = self._processing.autosave(
                        reference, grant, expected_version=version,
                        review=review, review_step="review-found",
                    )
                except ProfileIntakeError as exc:
                    return _failure(_save_error_code(exc.code))
                return _form_page_response(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    _review_page(reference, updated, csrf_secret,
                                 save_enabled=self._processing.durable_save_enabled,
                                 display_name_error=True),
                    script_sha256=_REVIEW_STATE_SCRIPT_HASH,
                    script_connect_self=True,
                )
            try:
                self._processing.save(
                    reference,
                    grant,
                    expected_version=version,
                    review=review,
                    request_digest=request_digest,
                )
            except ProfileIntakeError as exc:
                if exc.code == "review_field_invalid":
                    try:
                        _state, updated = self._processing.autosave(
                            reference, grant, expected_version=version,
                            review=review, review_step=review_step,
                        )
                    except ProfileIntakeError as save_error:
                        return _failure(_save_error_code(save_error.code))
                    # Post/redirect/get keeps reload from resubmitting an old
                    # version. GET derives field feedback from the owned draft;
                    # the URL never carries a field value or a trusted verdict.
                    location = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode(
                        {"draft": reference, "check": "1"}
                    )
                    return _response(
                        HTTPStatus.SEE_OTHER,
                        _message_page("Review saved", "Check the highlighted information before finishing."),
                        extra_headers=(("Location", location),),
                    )
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
    review = review_with_display_name_input(review)
    if type(allow_pending) is not bool:
        raise ProfileIntakeError("invalid_review_submission")
    expected = {"action", "version", "csrf"}
    if "review_step" in form:
        expected.add("review_step")
    confirm_step = _single(form, _REVIEW_CONFIRM_FIELD)
    if confirm_step is not None:
        if (
            confirm_step not in {_PROFILE_BASICS_REVIEW_STEP, _BACKGROUND_REVIEW_STEP}
            or _single(form, "action") not in {"autosave", "save", "update"}
        ):
            raise ProfileIntakeError("invalid_review_submission")
        expected.add(_REVIEW_CONFIRM_FIELD)
    confirm_profile_basics = confirm_step == _PROFILE_BASICS_REVIEW_STEP
    confirm_background = confirm_step == _BACKGROUND_REVIEW_STEP
    reset_section = _single(form, _SECTION_RESET_FIELD)
    if reset_section is not None:
        if (
            _single(form, "action") != "autosave"
            or confirm_step is not None
            or not review_reset_section_available(review, reset_section)
        ):
            raise ProfileIntakeError("invalid_review_submission")
        expected.add(_SECTION_RESET_FIELD)
    collection_updates, collection_fields = _review_collections_from_form(
        review,
        form,
        reset_section=reset_section,
    )
    expected.update(collection_fields)
    education_updates, education_fields = _education_entries_from_form(
        review,
        form,
        reset_section=reset_section,
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
        if (
            reset_section is not None
            and reset_section == _review_reset_section_for_fact(fact)
        ):
            value_name = f"fact_{index}_value"
            expected.add(value_name)
            if _single(form, value_name) is None:
                raise ProfileIntakeError("invalid_review_submission")
            if not _uses_direct_profile_fact(fact) and not (
                fact.field_path == "experience.total_years"
                and fact.conflict_group is None
            ):
                decision_name = f"fact_{index}_decision"
                expected.add(decision_name)
                if _single(form, decision_name) is None:
                    raise ProfileIntakeError("invalid_review_submission")
            values.append(review_value_for_form(fact.value))
            decisions.append(fact.decision)
            continue
        if fact.field_path in INTERNAL_INFERRED_CLASSIFICATION_FIELD_PATHS:
            values.append(review_value_for_form(fact.value))
            decisions.append(_server_internal_fact_decision(fact))
            continue
        if fact.field_path in _HIDDEN_BACKGROUND_FIELD_PATHS:
            values.append(review_value_for_form(fact.value))
            decisions.append(_server_hidden_background_fact_decision(fact))
            continue
        if fact.field_path.startswith("preferences."):
            values.append(review_value_for_form(fact.value))
            decisions.append(_server_legacy_preference_fact_decision(fact))
            continue
        if fact.field_path == "experience.total_years" and fact.conflict_group is None:
            value_name = f"fact_{index}_value"
            expected.add(value_name)
            value = _single(form, value_name)
            if value is None:
                raise ProfileIntakeError("invalid_review_submission")
            if not value.strip():
                value = review_value_for_form(fact.value)
                decision = "reject" if fact.suggested else "remove"
            elif fact.suggested:
                if fact.decision == "reject":
                    decision = "pending"
                else:
                    decision = fact.decision
                if decision == "pending" and not allow_pending and not confirm_background:
                    raise ProfileIntakeError("invalid_review_submission")
            else:
                decision = "keep"
            values.append(value)
            decisions.append(decision)
            continue
        if _uses_direct_profile_fact(fact):
            value_name = f"fact_{index}_value"
            expected.add(value_name)
            value = _single(form, value_name)
            if value is None:
                raise ProfileIntakeError("invalid_review_submission")
            if not value.strip():
                value = review_value_for_form(fact.value)
                decision = "reject" if fact.suggested else "remove"
            elif fact.suggested:
                changed = _parse_review_value(fact.field_path, value) != fact.value
                decision = "accept" if changed else fact.decision
                if (
                    decision == "pending"
                    and not allow_pending
                    and not confirm_profile_basics
                ):
                    raise ProfileIntakeError("invalid_review_submission")
            else:
                decision = "keep"
            values.append(value)
            decisions.append(decision)
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
            if (
                allow_pending
                and fact.suggested
                and fact.decision == "pending"
                and fact.field_path
                in {"experience.total_years", "experience.seniority"}
            ):
                decision = "pending"
            else:
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
        value = _single(form, key)
        if name == "display_name" and value is None:
            user_inputs[name] = ""
            continue
        expected.add(key)
        if value is None:
            raise ProfileIntakeError("invalid_review_submission")
        user_inputs[name] = value
    preference_model, preference_fields = _preference_model_from_form(
        form,
        review.preference_model,
    )
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
        confirm_background,
        confirm_profile_basics,
        reset_section,
    )


def _uses_direct_profile_fact(fact):
    """Ordinary visible facts are edited directly; conflicts keep explicit choices."""

    return fact.conflict_group is None and (
        not fact.suggested
        or fact.field_path.startswith(("identity.", "location."))
    )


def _server_internal_fact_decision(fact):
    """Retain unambiguous internal inference without browser confirmation."""

    if fact.field_path not in INTERNAL_INFERRED_CLASSIFICATION_FIELD_PATHS:
        raise ProfileIntakeError("invalid_review_submission")
    if fact.decision != "pending":
        return fact.decision
    return "reject" if fact.conflict_group is not None else "accept"


def _server_hidden_background_fact_decision(fact):
    """Leave new hidden suggestions unused without rewriting prior choices."""

    if fact.field_path not in _HIDDEN_BACKGROUND_FIELD_PATHS:
        raise ProfileIntakeError("invalid_review_submission")
    return "reject" if fact.decision == "pending" else fact.decision


def _server_legacy_preference_fact_decision(fact):
    """Keep Step 3 authoritative without rewriting an existing checkpoint choice."""

    if not fact.field_path.startswith("preferences."):
        raise ProfileIntakeError("invalid_review_submission")
    return "reject" if fact.decision == "pending" else fact.decision


def _education_entries_from_form(review, form, *, reset_section=None):
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
    if reset_section == "education":
        return tuple(
            (
                entry["value"]["kind"],
                entry["value"]["qualification"],
                entry["value"]["field"],
                entry["value"]["institution"],
                entry["value"]["status"],
                "" if entry["value"]["completion_year"] is None else str(entry["value"]["completion_year"]),
                entry["decision"],
            )
            for entry in education_entry_values(review)
        ), names
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


def _review_collections_from_form(review, form, *, reset_section=None):
    collection_names = {
        name for name in form if name.startswith("review_collection_")
    }
    if not collection_names:
        return None, set()
    updates = {}
    submitted_fields = set()
    matched_fields = set()
    for collection_id, spec in PROFILE_INTAKE_REVIEW_COLLECTIONS.items():
        if spec.get("browser_visible") is not True:
            continue
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
        existing_entries = review_collection_entries(review, collection_id)
        reset_collection = {
            "work_history": "job_titles",
            "languages": "languages",
            "expertise": "skills",
        }.get(reset_section)
        if collection_id == reset_collection:
            submitted_fields.update(
                name
                for name in collection_names
                if pattern.fullmatch(name) is not None
            )
            updates[collection_id] = tuple(
                (
                    entry["value"].language,
                    entry["value"].proficiency or "",
                    entry["value"].locale or "",
                    entry["decision"],
                )
                if spec["kind"] == "language"
                else (entry["value"], entry["decision"])
                for entry in existing_entries
            )
            continue
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
                if spec.get("include_suggested") is True and index < len(existing_entries):
                    entry = existing_entries[index]
                    if decision == "keep" and entry["suggested"]:
                        decision = (
                            "pending"
                            if entry["decision"] == "pending"
                            else "keep"
                        )
                items.append((value, decision))
        updates[collection_id] = tuple(items)
    if matched_fields != collection_names:
        raise ProfileIntakeError("invalid_review_submission")
    return updates, submitted_fields


def _collection_form_values_for_review(review):
    """Return the sole browser representation of current typed collections."""

    fields = {}
    for collection_id, spec in PROFILE_INTAKE_REVIEW_COLLECTIONS.items():
        if spec.get("browser_visible") is not True:
            continue
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


def _preference_model_from_form(form, current_model):
    """Build the sole authoritative model from closed server-owned controls."""

    if type(form) is not dict:
        raise ProfileIntakeError("invalid_review_submission")
    try:
        current = preference_model_for_v2_editor(current_model)
    except ProfilePreferenceModelError:
        raise ProfileIntakeError("invalid_review_submission") from None
    allow_existing_job_interests = bool(current["job_interests"])
    catalog = profile_preference_control_catalog_v2()
    model = empty_profile_preferences_v2()
    allowed_checkbox_fields = {}
    allowed_mode_fields = {}
    for dimension in catalog["dimensions"]:
        if dimension["id"] == "job_interests" and not allow_existing_job_interests:
            continue
        path = dimension["path"]
        mode_field = _preference_mode_field(path)
        allowed_mode_fields[mode_field] = path
        for choice in dimension["choices"]:
            field_name = _preference_choice_field(path, choice["code"])
            allowed_checkbox_fields[field_name] = (path, choice["code"])

    submitted = {_PREFERENCE_COMPENSATION_MARKER}
    if _single(form, _PREFERENCE_COMPENSATION_MARKER) != "present":
        raise ProfileIntakeError("invalid_review_submission")
    modes = {}
    for field_name, path in allowed_mode_fields.items():
        mode = _single(form, field_name)
        if mode not in {"unrestricted", "preferences"}:
            raise ProfileIntakeError("invalid_review_submission")
        modes[path] = mode
        submitted.add(field_name)
    for field_name, (path, code) in allowed_checkbox_fields.items():
        if field_name not in form:
            continue
        if _single(form, field_name) != "selected":
            raise ProfileIntakeError("invalid_review_submission")
        if modes[path] != "preferences":
            raise ProfileIntakeError("invalid_review_submission")
        parent = model
        for part in path[:-1]:
            parent = parent[part]
        parent[path[-1]].append(code)
        submitted.add(field_name)
    for path, mode in modes.items():
        if mode != "preferences":
            continue
        parent = model
        for part in path:
            parent = parent[part]
        if not parent:
            raise ProfileIntakeError("invalid_review_submission")

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


def _preference_mode_field(path):
    name = "preference_" + "_".join((*path, "mode"))
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
        if dimension["id"] == "job_interests" and not parent:
            continue
        fields[_preference_mode_field(path)] = (
            "preferences" if parent else "unrestricted"
        )
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


def _review_page(reference, snapshot, csrf_secret, *, save_enabled=False, display_name_error=False, validation_issue=None):
    snapshot = replace(snapshot, review=review_with_display_name_input(snapshot.review))
    fact_fields = []
    education_fact_indexes = managed_education_fact_indexes(snapshot.review)
    for index, fact in enumerate(snapshot.review.facts):
        if index in education_fact_indexes:
            continue
        if fact.field_path in _HIDDEN_BACKGROUND_FIELD_PATHS:
            continue
        label = (
            _UNPAIRED_EDUCATION_LABELS.get(fact.review_field)
            if snapshot.review.education_entries
            else None
        ) or _REVIEW_FIELD_LABELS.get(
            fact.review_field,
            fact.review_field.replace("_", " ").title(),
        )
        if fact.field_path == "experience.recent_roles":
            label = "Employment details — employer, dates and work arrangement"
        raw_value = review_value_for_form(fact.value)
        source_label = _review_source_label(fact)
        direct_profile_fact = _uses_direct_profile_fact(fact)
        if direct_profile_fact:
            choice = ""
            badge = ""
        elif _uses_compact_suggestion_choice(fact):
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
        if fact.field_path in INTERNAL_INFERRED_CLASSIFICATION_FIELD_PATHS:
            continue
        if fact.field_path.startswith("preferences."):
            continue
        value_control = _review_fact_value_control(index, fact, raw_value, label)
        if direct_profile_fact:
            pending = fact.suggested and fact.decision == "pending"
            card = (
                f"<div class='direct-profile-fact'{' data-profile-basics-pending=true' if pending else ''}>"
                f"{value_control}</div>"
            )
            compact_item = card
        else:
            pending = fact.decision == "pending"
            card = (
                f"<article class='profile-group fact-card'{' data-profile-basics-pending=true' if pending and fact.conflict_group is not None else ''}>"
                f"<p class='fact-meta'>{_safe_text(badge)}</p>{value_control}{choice}</article>"
            )
            compact_item = (
                f"<div class='fact-group-item'><p class='fact-meta'>{_safe_text(badge)}</p>"
                f"{value_control}{choice}</div>"
            )
        fact_fields.append((fact, card, compact_item))
    missing = []
    existing_inputs = dict(snapshot.review.user_inputs)
    for name in snapshot.review.missing_user_fields:
        if name == "display_name":
            continue
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
        "This creates your Wahojobs profile using the information you reviewed. You can update it later."
        if save_enabled
        else "You can update this preview, but it cannot be saved here."
    )
    step_four_summary = _render_step_four_summary(snapshot.review)
    autosave_form = (
        f"<form id='profile-review-autosave' method='post' action='{target}' hidden>"
        f"<input type='hidden' name='action' value='autosave'><input type='hidden' name='version' value='{snapshot.version}'><input type='hidden' name='csrf' value='{autosave_proof}'>"
        "</form>"
        if save_enabled
        else ""
    )
    about_facts = tuple(
        item
        for item in fact_fields
        if item[0].field_path.startswith(("identity.", "location."))
    )
    other_facts = tuple(
        item
        for item in fact_fields
        if item not in about_facts
        and item[0].field_path != "experience.industries"
    )
    about_cards = _render_found_fact_cards(about_facts)
    if "display_name" in snapshot.review.missing_user_fields:
        about_cards = (
            "<label class='review-field'><span>Display name</span>"
            "<small>What would you like us to call you? This doesn’t need to be your legal name.</small>"
            "<input id='profile-display-name' data-display-name name='missing_display_name' "
            f"value='{_safe_text(existing_inputs.get('display_name', ''))}' maxlength='160' required "
            "aria-describedby='display-name-error'></label>"
        ) + about_cards
    name_feedback = (
        f"<p id='display-name-error' role='alert'{' hidden' if not display_name_error else ''}>"
        "Enter a display name of 1–160 characters, without contact details. "
        "<a href='#review-found' data-focus-display-name>Go to display name</a></p>"
    )
    about_cards = name_feedback + about_cards
    remaining_found_cards = _render_found_fact_cards(other_facts)
    about_section = (
        "<section class='review-concept-section review-about-you' aria-labelledby='review-about-you-title'>"
        "<div class='review-concept-heading'><h3 id='review-about-you-title'>About you</h3>"
        "<p>Check that these details are correct. Based in means where you currently live; work eligibility is handled separately.</p></div>"
        f"<div class='profile-basics-grid'>{about_cards}</div>{_render_section_reset(snapshot.review, 'profile_basics')}</section>"
        if about_cards
        else ""
    )
    other_facts_section = (
        "<section class='review-concept-section review-other-facts' aria-labelledby='review-other-details-title'>"
        "<div class='review-concept-heading'><h3 id='review-other-details-title'>Other details</h3></div>"
        f"<div class='profile-grid'>{remaining_found_cards}</div></section>"
        if remaining_found_cards
        else ""
    )
    experience_collection = _render_review_collection(
        snapshot.review,
        "job_titles",
    )
    education_collection = _render_education_entries(snapshot.review)
    skills_collection = _render_review_collection(snapshot.review, "skills")
    languages_collection = _render_review_collection(snapshot.review, "languages")
    experience_summary_items = "".join(
        _render_compact_experience_fact(
            next(
                index
                for index, current in enumerate(snapshot.review.facts)
                if current is fact
            ),
            fact,
        )
        for fact, _card, _compact_item in fact_fields
        if fact.conflict_group is None
        and fact.field_path == "experience.total_years"
    )
    experience_summary = (
        "<section class='review-concept-section compact-experience-section' aria-labelledby='compact-experience-title'>"
        "<div class='review-concept-heading'><h3 id='compact-experience-title'>Professional experience</h3></div>"
        f"<div class='compact-experience-grid'>{experience_summary_items}</div>{_render_section_reset(snapshot.review, 'professional_experience')}</section>"
        if experience_summary_items
        else ""
    )
    experience_suggestion_cards = "".join(
        card
        for fact, card, _compact_item in fact_fields
        if fact.suggested
        and fact.conflict_group is None
        and fact.field_path.startswith("experience.")
        and fact.field_path
        not in {
            "experience.industries",
            "experience.specialties",
            "experience.total_years",
            "experience.seniority",
        }
        and not _is_managed_collection_fact(fact)
    )
    other_suggestion_cards = "".join(
        card
        for fact, card, _compact_item in fact_fields
        if fact.suggested
        and fact.conflict_group is None
        and not fact.field_path.startswith("experience.")
    )
    suggestion_sections = []
    if experience_suggestion_cards:
        suggestion_sections.append(
            "<section class='review-concept-section' aria-labelledby='review-experience-suggestions-title'>"
            "<div class='review-concept-heading'><h3 id='review-experience-suggestions-title'>Other background details</h3>"
            "<p>Check any additional details interpreted from your documents.</p></div>"
            f"<div class='profile-grid'>{experience_suggestion_cards}</div></section>"
        )
    if other_suggestion_cards:
        suggestion_sections.append(
            "<section class='review-concept-section' aria-labelledby='review-other-suggestions-title'>"
            "<div class='review-concept-heading'><h3 id='review-other-suggestions-title'>Other profile details</h3></div>"
            f"<div class='profile-grid'>{other_suggestion_cards}</div></section>"
        )
    suggestion_content = "".join(suggestion_sections)
    conflict_cards = "".join(
        card
        for fact, card, _compact_item in fact_fields
        if fact.conflict_group is not None
    )
    conflict_section = (
        "<div class='review-subsection review-conflicts'><h3>Sources disagree — please confirm</h3>"
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
    matching_explanation = (
        "<details class='matching-explanation-disclosure'>"
        "<summary><span class='matching-explanation-icon' aria-hidden='true'>&#9432;</span>"
        "<span>How your preferences affect matches</span></summary>"
        "<div class='matching-explanation-body'>"
        "<p><strong>We use your preferences whenever a job gives us enough information to compare.</strong></p>"
        "<p>For example, if you prefer full-time work and a job is clearly part-time, it may not appear in your main matches. "
        "If a job doesn’t say whether it’s full-time or part-time, we won’t assume it conflicts with your preference.</p>"
        "<p><strong>Your background works differently.</strong> We use your experience, skills, languages, and education to find "
        "AI training and evaluation opportunities that fit you. You don’t need to choose every job area or task type yourself.</p>"
        "</div></details>"
    )
    field_feedback = _review_field_feedback(validation_issue)
    country_options = "<datalist id='profile-country-options'>" + "".join(
        f"<option value='{_safe_text(country)}'></option>" for country in CANONICAL_COUNTRIES
    ) + "</datalist>"
    source_kinds = {source.document_kind for source in snapshot.review.sources}
    if source_kinds == {DocumentKind.RESUME}:
        profile_basics_copy = "Found in your resume. Check the details and add anything important we missed."
    elif source_kinds == {DocumentKind.LINKEDIN_PROFILE_EXPORT}:
        profile_basics_copy = "Found in your LinkedIn profile. Check the details and add anything important we missed."
    else:
        profile_basics_copy = "Found in your documents. Check the details and add anything important we missed."
    body = f"""
    {_authenticated_navigation()}
    <section class='profile-header intake-hero intake-review-hero'><p class='eyebrow'>Your Wahojobs profile draft</p><h1>Review it and make it yours</h1>
      <p class='hero-lede'>We organized what your documents say. You decide what belongs in your profile.</p>
      <p class='reassurance-line'><span aria-hidden='true'>&#10003;</span> Your progress is saved as you review. No profile is created until you finish.</p>
      <nav class='review-progress' aria-label='Profile review steps'><ol>
        <li><a href='#review-found'><span>1</span><span class='review-progress-label'>Profile basics<small data-step-attention hidden>Needs attention</small></span></a></li>
        <li><a href='#review-suggestions'><span>2</span><span class='review-progress-label'>Skills &amp; experience<small data-step-attention hidden>Needs attention</small></span></a></li>
        <li><a href='#review-preferences'><span>3</span><span class='review-progress-label'>What you want<small data-step-attention hidden>Needs attention</small></span></a></li>
        <li><a href='#review-finish'><span>4</span><span class='review-progress-label'>Find matches<small data-step-attention hidden>Needs attention</small></span></a></li>
      </ol></nav>
    </section>
    {issue_note}
    <p id='profile-review-lifetime' class='intake-callout review-expiry-warning' role='status' aria-live='polite' hidden></p>
    <div id='profile-review-save-status' class='review-save-status' data-state='saved' role='status' aria-live='polite'>
      <span class='save-status-dot' aria-hidden='true'></span><span id='profile-review-save-label'>{'Progress saved' if save_enabled else 'Review active'}</span>
      <button id='profile-review-save-retry' class='button-quiet' type='button' hidden>Retry</button>
      <a id='profile-review-save-resume' href='{PROFILE_INTAKE_ROUTE}' hidden>Continue saved progress</a>
    </div>
    <form id='profile-review-form' class='profile-review-form intake-review-form' method='post' action='{target}' novalidate>
      {field_feedback}{country_options}
      <input type='hidden' name='action' value='{primary_action}'><input type='hidden' name='version' value='{snapshot.version}'><input type='hidden' name='csrf' value='{primary_proof}'><input type='hidden' name='review_step' value='{_safe_text(snapshot.review_step)}'><input type='hidden' name='{_REVIEW_CONFIRM_FIELD}' value='' disabled><input type='hidden' name='{_SECTION_RESET_FIELD}' value='' data-section-reset-field disabled>
      <section class='review-section' id='review-found' aria-labelledby='review-found-title'><div class='section-heading'><p class='eyebrow'>Step 1 of 4</p><h2 id='review-found-title'>Your profile basics</h2><p>{_safe_text(profile_basics_copy)}</p></div>
        <div class='review-profile-sections'>
          {about_section}
          <section class='review-concept-section' aria-labelledby='review-experience-title'><div class='review-concept-heading'><h3 id='review-experience-title'>Work history</h3><p>Roles found in your documents. Edit or remove anything that isn’t right.</p></div>{experience_collection}</section>
          {education_collection}
          {languages_collection}
          {other_facts_section}
          {conflict_section}
        </div><div class='background-review-continue'><button type='button' data-confirm-profile-basics>Continue</button></div>
      </section>
      <section class='review-section' id='review-suggestions' aria-labelledby='review-suggestions-title'><div class='section-heading'><p class='eyebrow'>Step 2 of 4</p><h2 id='review-suggestions-title'>Skills &amp; experience</h2><p>Review the skills and expertise we found and add anything important we missed.</p></div>
        <section class='review-concept-section expertise-review-section' aria-labelledby='expertise-review-title'><div class='review-concept-heading'><h3 id='expertise-review-title'>Skills and areas of expertise</h3></div>{skills_collection}</section>
        {experience_summary}{suggestion_content}<div class='background-review-continue'><button type='button' data-confirm-background>Continue</button></div></section>
      <section class='review-section' id='review-preferences' aria-labelledby='review-preferences-title'><div class='section-heading'><p class='eyebrow'>Step 3 of 4</p><h2 id='review-preferences-title'>What are you looking for?</h2><p>Choose the work conditions you would consider.</p></div>{matching_explanation}{_render_preference_controls(snapshot.review.preference_model)}
        {missing_section}</section>
      <section class='review-section finish-section' id='review-finish' aria-labelledby='review-finish-title'><div class='finish-panel'><p class='eyebrow'>Step 4 of 4</p><h2 id='review-finish-title'>Ready to find matches</h2><p>Wahojobs will use the background and work preferences you reviewed to find relevant opportunities.</p>{step_four_summary}<div class='finish-actions'><button type='submit'>{primary_label}</button><span class='muted'>{persistence_note}</span></div></div></section>
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


def _review_field_feedback(issue):
    if not issue:
        return ""
    index = issue.get("index")
    if issue.get("kind") == "review":
        return (
            "<p id='profile-field-error' class='intake-callout' role='alert'>"
            "Your draft is saved, but some information still needs correction before you finish. "
            "<a href='#review-found'>Review your details</a></p>"
        )
    if type(index) is not int or index < 0:
        return ""
    if issue.get("kind") == "skill":
        target = f"review-collection-skills-{index}-value"
        message = (
            (f"This item is too long ({issue['limit']} characters maximum). "
             if issue.get("reason") == "length" else "Remove unsupported characters from this item. ")
            + "If it lists several skills, use Add another skill or area of expertise "
            "to enter them separately."
        )
        label = "Review this item"
    elif issue.get("kind") == "country":
        target = f"review-fact-{index}-value"
        message = (
            "Enter one country name or two-letter code, for example Brazil or BR. "
            "If you are not sure which country applies, keep your draft and confirm it before finishing."
        )
        label = "Review this country"
    else:
        return ""
    return (
        "<p id='profile-field-error' class='intake-callout' role='alert'>"
        f"{_safe_text(message)} <a href='#{target}' data-review-error-target='{target}'>"
        f"{label}</a></p>"
    )


def _step_four_summary(review):
    """Return a bounded, non-authoritative view of the current reviewed state."""

    location_parts = []
    residence = _active_review_strings(review, "location.residence")
    if residence:
        location_parts.extend(residence[:1])
    else:
        for path in ("location.city", "location.region", "location.country"):
            location_parts.extend(_active_review_strings(review, path)[:1])
    location = ", ".join(_unique_summary_strings(location_parts))

    title_values = _active_collection_strings(review, "job_titles")
    title_summary = _bounded_named_values(
        title_values,
        visible=_STEP_FOUR_MAX_JOB_TITLES,
        singular="job title",
        plural="job titles",
    )
    education = _active_education_summary(review)

    language_names = []
    for entry in review_collection_entries(review, "languages"):
        if entry["decision"] != "keep" or type(entry["value"]) is not LanguageValue:
            continue
        language_names.append(entry["value"].language)
    language_summary = _bounded_named_values(
        _unique_summary_strings(language_names),
        visible=_STEP_FOUR_MAX_LANGUAGES,
        singular="language",
        plural="languages",
    )

    skill_count = len(_active_collection_strings(review, "skills"))
    skill_summary = (
        "1 skill or area of expertise"
        if skill_count == 1
        else f"{skill_count} skills and areas of expertise"
        if skill_count
        else ""
    )
    background = tuple(
        value
        for value in (location, title_summary, education, language_summary, skill_summary)
        if value
    )

    preference_rows, compensation_rows = _step_four_preference_rows(review)
    if not preference_rows:
        preference_rows = ("No specific work-condition preferences",)

    inputs = dict(review.user_inputs)
    important = []
    for field, label in (
        ("work_authorization", "Work authorization"),
        ("eligible_countries", "Can work in"),
        ("geographic_restrictions", "Location limits"),
        ("hard_constraints", "Firm limits"),
    ):
        value = _bounded_step_four_text(inputs.get(field, ""))
        if value:
            important.append(f"{label}: {value}")
    if _bounded_step_four_text(inputs.get("accessibility_constraints", "")):
        important.append("Accessibility needs added")

    return {
        "background": background,
        "preferences": tuple((*preference_rows, *compensation_rows)),
        "important": tuple(important),
    }


def _render_step_four_summary(review):
    summary = _step_four_summary(review)

    def values(items):
        return "".join(f"<li>{_safe_text(item)}</li>" for item in items)

    important = (
        "<section class='step-four-summary-section step-four-important' aria-labelledby='step-four-important-title'>"
        "<h3 id='step-four-important-title'>Important details</h3>"
        f"<ul class='step-four-summary-values'>{values(summary['important'])}</ul></section>"
        if summary["important"]
        else ""
    )
    return (
        "<div class='step-four-summary'>"
        "<section class='step-four-summary-section' aria-labelledby='step-four-background-title'>"
        "<h3 id='step-four-background-title'>Your background</h3>"
        f"<ul class='step-four-summary-values'>{values(summary['background'])}</ul>"
        "<nav class='step-four-edit-actions' aria-label='Edit your background'>"
        "<a href='#review-found'>Edit profile basics</a>"
        "<a href='#review-suggestions'>Edit skills &amp; experience</a></nav></section>"
        "<section class='step-four-summary-section' aria-labelledby='step-four-preferences-title'>"
        "<h3 id='step-four-preferences-title'>Work preferences</h3>"
        f"<ul class='step-four-summary-values'>{values(summary['preferences'])}</ul>"
        "<nav class='step-four-edit-actions' aria-label='Edit your work preferences'>"
        "<a href='#review-preferences'>Edit work preferences</a></nav></section>"
        f"{important}</div>"
    )


def _active_review_strings(review, field_path):
    return _unique_summary_strings(
        fact.value
        for fact in review.facts
        if fact.field_path == field_path
        and fact.decision in {"keep", "accept"}
        and type(fact.value) is str
    )


def _active_collection_strings(review, collection_id):
    return _unique_summary_strings(
        entry["value"]
        for entry in review_collection_entries(review, collection_id)
        if entry["decision"] == "keep" and type(entry["value"]) is str
    )


def _unique_summary_strings(values):
    result = []
    seen = set()
    for value in values:
        if type(value) is not str:
            continue
        candidate = " ".join(value.split())
        identity = candidate.casefold()
        if not candidate or identity in seen:
            continue
        seen.add(identity)
        result.append(candidate)
    return tuple(result)


def _bounded_named_values(values, *, visible, singular, plural):
    values = tuple(values)
    if not values:
        return ""
    shown = values[:visible]
    if len(values) == 1:
        return shown[0]
    if len(values) <= visible:
        return ", ".join(shown[:-1]) + " and " + shown[-1]
    remaining = len(values) - visible
    label = singular if remaining == 1 else plural
    return ", ".join(shown) + f" +{remaining} more {label}"


def _active_education_summary(review):
    entries = tuple(
        entry["value"]
        for entry in education_entry_values(review)
        if entry["decision"] == "keep"
    )
    if len(entries) > 1:
        return f"{len(entries)} education entries reviewed"
    if len(entries) == 1:
        entry = entries[0]
        for value in (
            entry["qualification"],
            _EDUCATION_KIND_LABELS.get(entry["kind"], "")
            if entry["kind"] != "not_specified"
            else "",
            entry["field"],
            entry["institution"],
        ):
            if type(value) is str and value.strip():
                return " ".join(value.split())
    legacy = _active_review_strings(review, "education.degrees")
    if len(legacy) == 1:
        return legacy[0]
    if len(legacy) > 1:
        return f"{len(legacy)} education qualifications reviewed"
    return ""


def _step_four_preference_rows(review):
    model = preference_model_for_v2_editor(review.preference_model)
    catalog = profile_preference_control_catalog_v2()
    rows = []
    for dimension in catalog["dimensions"]:
        container = model
        for part in dimension["path"]:
            container = container[part]
        if not container:
            continue
        labels = {
            choice["code"]: choice["label"] for choice in dimension["choices"]
        }
        selected = tuple(labels[code] for code in container)
        value = _bounded_named_values(
            selected,
            visible=3,
            singular="choice",
            plural="choices",
        )
        title = (
            "Existing job interests"
            if dimension["id"] == "job_interests"
            else dimension["title"]
        )
        rows.append(f"{title}: {value}")
    if len(rows) > _STEP_FOUR_MAX_PREFERENCE_ROWS:
        remaining = len(rows) - (_STEP_FOUR_MAX_PREFERENCE_ROWS - 1)
        rows = rows[: _STEP_FOUR_MAX_PREFERENCE_ROWS - 1]
        rows.append(f"{remaining} more preferences reviewed")

    compensation = tuple(
        f"{expectation['minimum_kind'].title()} minimum: "
        f"{expectation['currency']} {expectation['amount']}/{expectation['period']}"
        for expectation in model["compensation_expectations"]
    )
    return tuple(rows), compensation


def _bounded_step_four_text(value):
    if type(value) is not str:
        return ""
    value = " ".join(value.split())
    if len(value) <= _STEP_FOUR_DETAIL_LIMIT:
        return value
    return value[: _STEP_FOUR_DETAIL_LIMIT - 1].rstrip() + "…"


def _render_found_fact_cards(fact_fields):
    grouped_counts = {}
    for fact, _card, _compact_item in fact_fields:
        if (
            fact.suggested
            or fact.conflict_group is not None
            or _is_managed_collection_fact(fact)
        ):
            continue
        if fact.field_path != "experience.recent_roles" and fact.review_field in _COMPACT_FOUND_REVIEW_FIELDS:
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
    if fact.conflict_group is not None:
        return False
    return any(
        fact.field_path in spec["paths"]
        and (not fact.suggested or spec.get("include_suggested") is True)
        for spec in PROFILE_INTAKE_REVIEW_COLLECTIONS.values()
    )


_SECTION_RESET_COPY = {
    "profile_basics": ("profile-basics", "profile basics"),
    "work_history": ("work-history", "work history"),
    "education": ("education", "education"),
    "languages": ("languages", "languages"),
    "expertise": ("expertise", "skills & expertise"),
    "professional_experience": ("professional-experience", "professional experience"),
}


def _render_section_reset(review, section_id):
    """Render one quiet, explicit recovery action for a proven extraction baseline."""

    if not review_reset_section_available(review, section_id):
        return ""
    slug, noun = _SECTION_RESET_COPY[section_id]
    panel_id = f"{slug}-reset-confirm"
    title_id = f"{slug}-reset-title"
    return (
        "<div class='expertise-reset section-reset'>"
        f"<button class='button-quiet expertise-reset-open' type='button' data-section-reset-open aria-expanded='false' aria-controls='{panel_id}'>Reset to Wahojobs suggestions</button>"
        f"<div class='expertise-reset-confirm section-reset-confirm' id='{panel_id}' data-section-reset-confirm role='group' aria-labelledby='{title_id}' hidden>"
        f"<p id='{title_id}'><strong>Reset {_safe_text(noun)}?</strong></p>"
        "<p>This will restore Wahojobs' original suggestions and remove changes you've made in this section.</p>"
        "<div class='expertise-reset-actions'><button class='button-quiet' type='button' data-section-reset-cancel>Cancel</button>"
        f"<button type='button' data-section-reset-confirm-action='{_safe_text(section_id)}'>Reset</button></div></div></div>"
    )


def _render_compact_experience_fact(index, fact):
    if fact.field_path != "experience.total_years":
        raise ProfileIntakeError("invalid_review_submission")
    pending = fact.suggested and fact.decision == "pending"
    removed = fact.decision in {"reject", "remove"}
    raw_value = "" if removed else review_value_for_form(fact.value)
    return (
        f"<div class='compact-experience-item'{' data-background-pending=true' if pending else ''}>"
        "<label class='review-field'><span>Approx. years of professional experience</span>"
        f"<input name='fact_{index}_value' value='{_safe_text(raw_value)}' inputmode='decimal' maxlength='512' aria-describedby='professional-years-help'>"
        "<small id='professional-years-help'>Optional. Clear the field if you prefer not to include it.</small></label></div>"
    )


def _render_review_collection(review, collection_id):
    spec = PROFILE_INTAKE_REVIEW_COLLECTIONS[collection_id]
    entries = review_collection_entries(review, collection_id)
    items = "".join(
        _render_review_collection_item(collection_id, spec, index, entry)
        for index, entry in enumerate(entries)
    )
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
            "requires_confirmation": False,
            "suggested": False,
            "mixed_decisions": False,
            "source_attributions": (),
            "members": (),
            "field_paths": (spec["add_path"],),
        },
        template=True,
    )
    visible_entry_count = sum(entry["decision"] != "remove" for entry in entries)
    empty = (
        "<p class='collection-empty'>Nothing listed yet. Add an item if it belongs in your profile.</p>"
        if not visible_entry_count
        else "<p class='collection-empty' hidden>Nothing listed yet. Add an item if it belongs in your profile.</p>"
    )
    noun = {
        "skills": "skill or area of expertise",
        "job_titles": "job title",
        "industries": "industry",
        "languages": "language",
    }[collection_id]
    help_text = {
        "skills": "Items shown will be included. Remove anything that is wrong. You don't need to list job types or every kind of AI work—Wahojobs uses your background to figure out where you may fit.",
        "job_titles": "Edit the roles we found, or add another role you have held.",
        "industries": "Add industries where you have real work experience. You do not need to list every possible area.",
        "languages": "Edit the languages we found or add another. Leave proficiency blank when you do not want to state one.",
    }[collection_id]
    active_add_path_count = sum(
        any(
            origin == "user"
            or review.facts[member_index].field_path == spec["add_path"]
            for origin, member_index in entry["members"]
        )
        and entry["decision"] != "remove"
        for entry in entries
    )
    add_capacity = spec.get("add_limit", spec["limit"]) - active_add_path_count
    browser_limit = min(spec["limit"], len(entries) + max(0, add_capacity))
    legend_class = (
        " class='screen-reader-only'"
        if collection_id in {"skills", "industries"}
        else ""
    )
    collection_actions = ""
    if collection_id == "skills":
        collection_actions = (
            "<p class='expertise-undo' data-expertise-undo role='status' aria-live='polite' hidden>"
            "<span data-expertise-undo-message></span> "
            "<button class='button-quiet' type='button' data-expertise-undo-action>Undo</button></p>"
            + _render_section_reset(review, "expertise")
        )
    elif collection_id == "job_titles":
        collection_actions = (
            "<p class='expertise-undo' data-work-history-undo role='status' aria-live='polite' hidden>"
            "<span data-work-history-undo-message></span> "
            "<button class='button-quiet' type='button' data-work-history-undo-action>Undo</button></p>"
            + _render_section_reset(review, "work_history")
        )
    elif collection_id == "languages":
        collection_actions = _render_section_reset(review, "languages")
    return (
        f"<fieldset class='review-collection {collection_id}-collection' data-review-collection='{collection_id}' data-next-index='{len(entries)}' data-limit='{browser_limit}'>"
        f"<legend{legend_class}>{_safe_text(spec['title'])}</legend><p class='muted'>{_safe_text(help_text)}</p>"
        f"{empty}<div class='review-collection-items' data-collection-items>{items}</div>"
        f"<button class='button-quiet collection-add' type='button' data-collection-add{' hidden' if browser_limit <= len(entries) else ''}>Add another {_safe_text(noun)}</button>"
        f"{collection_actions}<template data-collection-template>{template_item}</template></fieldset>"
    )


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
        if not any(entry["decision"] != "remove" for entry in entries)
        else "<p class='collection-empty' hidden>Nothing listed yet. Add your education if you would like it in your profile.</p>"
    )
    return (
        "<div class='review-collections education-review-collections' aria-label='Education'>"
        f"<fieldset class='review-collection education-collection' data-review-collection='education' data-next-index='{len(entries)}' data-limit='{MAX_EDUCATION_ENTRIES}'>"
        "<legend>Education</legend><p class='muted'>Edit the education we found or add another. Keep each school, degree, or course together so its status and completion year are clear.</p>"
        f"{empty}<div class='review-collection-items' data-collection-items>{items}</div>"
        "<button class='button-quiet collection-add' type='button' data-collection-add>Add another education entry</button>"
        f"{_render_section_reset(review, 'education')}<template data-collection-template>{template}</template></fieldset></div>"
    )


def _render_education_entry(index, entry, *, template=False):
    value = entry["value"]
    prefix = f"review_education_{index}_"
    item_id = f"review-education-{index}"
    removed = entry["decision"] == "remove"
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
        f"<div class='review-collection-item education-entry' data-collection-item data-index='{index}'{new_attribute}{' hidden aria-hidden=true' if removed else ''}>"
        "<div class='collection-item-controls education-entry-controls'>"
        f"<label class='review-field'><span>Education type</span><select id='{item_id}-kind' name='{prefix}kind'>{kind_options}</select></label>"
        f"<label class='review-field'><span>Qualification or course</span><input id='{item_id}-qualification' name='{prefix}qualification' value='{_safe_text(value['qualification'])}' maxlength='128'></label>"
        f"<label class='review-field'><span>Field of study</span><input id='{item_id}-field' name='{prefix}field' value='{_safe_text(value['field'])}' maxlength='128'></label>"
        f"<label class='review-field'><span>School or institution</span><input id='{item_id}-institution' name='{prefix}institution' value='{_safe_text(value['institution'])}' maxlength='128'></label>"
        f"<label class='review-field'><span>Status</span><select id='{item_id}-status' name='{prefix}status'>{status_options}</select></label>"
        f"<label class='review-field'><span>Completion year <small>(optional)</small></span><input id='{item_id}-completion-year' name='{prefix}completion_year' value='{_safe_text(year)}' inputmode='numeric' pattern='[0-9]{{4}}' maxlength='4'></label>"
        "</div>"
        f"<p class='collection-item-error' id='{item_id}-error' data-collection-error role='alert' hidden></p>"
        f"<input type='checkbox' name='{prefix}remove' value='remove'{' checked' if removed else ''} data-collection-remove hidden>"
        "<button class='collection-remove-action' type='button' data-collection-remove-action>Remove</button></div>"
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
    elif collection_id in {"skills", "industries"}:
        label = (
            "Skill or area of expertise"
            if collection_id == "skills"
            else "Industry"
        )
        size = min(40, max(18, len(entry["value"]) + 1))
        if entry.get("suggested"):
            controls = (
                f"<p class='compact-expertise-text'>{_safe_text(entry['value'])}</p>"
                f"<input type='hidden' name='{prefix}value' value='{_safe_text(entry['value'])}'>"
            )
        else:
            controls = (
                f"<label class='review-field compact-expertise-value'><span class='screen-reader-only'>{label}</span>"
                f"<input id='{item_id}-value' name='{prefix}value' value='{_safe_text(entry['value'])}' "
                f"maxlength='512' size='{size}' aria-label='{_safe_text(label)}' required></label>"
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
    compact_background = collection_id == "skills"
    token_item_class = " expertise-compact-row" if compact_background else ""
    if compact_background:
        pending = bool(entry.get("requires_confirmation") and not removed)
        remove_name = _safe_text(f"Remove {entry['value']}")
        return (
            f"<div class='review-collection-item{token_item_class}' data-collection-item data-expertise-item data-index='{index}'{new_attribute}{' data-background-pending=true' if pending else ''}{' hidden aria-hidden=true' if removed else ''}>"
            f"<div class='compact-row-main'>{controls}<label class='collection-remove' aria-label='{remove_name}'>"
            f"<input type='checkbox' name='{prefix}remove' value='remove'{' checked' if removed else ''} data-collection-remove>"
            "<span class='collection-remove-symbol' aria-hidden='true'>×</span><span class='remove-copy screen-reader-only'>Remove</span></label></div>"
            f"<p class='collection-item-error' id='{item_id}-error' data-collection-error role='alert' hidden></p></div>"
        )
    return (
        f"<div class='review-collection-item{token_item_class}' data-collection-item data-index='{index}'{new_attribute}{' hidden aria-hidden=true' if removed else ''}>"
        f"<div class='collection-item-controls'>{controls}</div>"
        f"<p class='collection-item-error' id='{item_id}-error' data-collection-error role='alert' hidden></p>"
        f"<input type='checkbox' name='{prefix}remove' value='remove'{' checked' if removed else ''} data-collection-remove hidden>"
        "<button class='collection-remove-action' type='button' data-collection-remove-action>Remove</button></div>"
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
        if _uses_direct_profile_fact(fact) and fact.decision in {"remove", "reject"}:
            raw_value = ""
        required = " required" if fact.field_path == "identity.display_name" else ""
        maxlength = "160" if fact.field_path == "identity.display_name" else "512"
        name_attributes = (
            f" id='profile-display-name-{index}' data-display-name aria-describedby='display-name-error'"
            if fact.field_path == "identity.display_name" else ""
        )
        if fact.field_path == "identity.display_name":
            label = "Display name"
        if fact.field_path == "location.residence":
            label = "Country of residence"
        if fact.field_path in {"location.country", "location.residence"}:
            name_attributes += f" id='review-fact-{index}-value' list='profile-country-options'"
        return (
            f"<label class='review-field'><span>{_safe_text(label)}</span>"
            f"<input name='fact_{index}_value' value='{_safe_text(raw_value)}' maxlength='{maxlength}'{required}{name_attributes}></label>"
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
        if dimension["id"] == "job_interests" and not selected:
            continue
        choice_by_code = {choice["code"]: choice for choice in dimension["choices"]}
        ordered_choices = tuple(dimension["choices"])
        visible_choices = ordered_choices
        more_choices = ()
        if dimension["id"] == "job_interests":
            visible_codes = [
                code
                for code in _COMMON_JOB_INTEREST_CODES
                if code in choice_by_code
            ]
            visible_codes.extend(
                choice["code"]
                for choice in ordered_choices
                if choice["code"] in selected
                and choice["code"] not in visible_codes
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
            description = (
                ""
                if dimension["id"] == "job_interests"
                else f"<small>{_safe_text(choice['description'])}</small>"
            )
            return (
                f"<label class='choice-card' for='{choice_id}'>"
                f"<input id='{choice_id}' type='checkbox' name='{field_name}' value='selected'"
                f"{' checked' if choice['code'] in selected else ''}>"
                f"<span><strong>{_safe_text(choice['label'])}</strong>{description}</span></label>"
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
            "<p class='selection-summary' data-selection-summary aria-live='polite'"
            f"{' hidden' if not selected_labels else ''}><strong>Selected:</strong> "
            f"<span data-selection-values>{_safe_text(', '.join(compact_selected_labels))}</span></p>"
            if dimension["id"] == "job_interests"
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
        local_help = (
            "<details class='choice-help'><summary>How to choose Job Interests</summary>"
            "<p>These interests were already confirmed in your profile and still shape your matches. Keep, change, or clear them here.</p></details>"
            if dimension["id"] == "job_interests"
            else ""
        )
        mode_field = _preference_mode_field(dimension["path"])
        mode_slug = mode_field.replace("_", "-")
        options_id = mode_slug + "-options"
        error_id = mode_slug + "-error"
        has_preferences = bool(selected)
        mode_controls = (
            "<div class='choice-grid preference-mode-grid' data-preference-mode-controls>"
            f"<label class='choice-card' for='{mode_slug}-unrestricted'>"
            f"<input id='{mode_slug}-unrestricted' type='radio' name='{mode_field}' value='unrestricted' data-preference-mode"
            f"{' checked' if not has_preferences else ''} required>"
            "<span><strong>No preference</strong><small>I’m open to any.</small></span></label>"
            f"<label class='choice-card' for='{mode_slug}-preferences'>"
            f"<input id='{mode_slug}-preferences' type='radio' name='{mode_field}' value='preferences' data-preference-mode aria-controls='{options_id}'"
            f"{' checked' if has_preferences else ''} required>"
            "<span><strong>I have preferences</strong><small>Show the choices I can select.</small></span></label></div>"
        )
        option_controls = (
            f"<div class='preference-option-panel' id='{options_id}' data-preference-options"
            f"{' hidden' if not has_preferences else ''}>"
            "<span class='selection-hint'>Choose all that apply</span>"
            f"<p class='muted' id='{help_id}'>{_safe_text(dimension['help'])}</p>"
            f"{selection_summary}<div class='choice-grid'>{''.join(choice_markup(choice) for choice in visible_choices)}</div>"
            f"{more_markup}{local_help}</div>"
            f"<p class='preference-mode-error' id='{error_id}' data-preference-mode-error role='alert' hidden>Choose at least one option, or select No preference.</p>"
        )
        section = (
            f"<fieldset class='preference-group{' job-interest-group' if dimension['id'] == 'job_interests' else ''}' aria-describedby='{help_id}' data-preference-dimension='{_safe_text(dimension['id'])}'>"
            f"<legend>{_safe_text(dimension['title'])}</legend>{mode_controls}{option_controls}</fieldset>"
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
        f"<div class='disclosure-body'>{''.join(secondary_sections)}</div></details>"
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
        f"<p class='collection-item-error' id='{item_id}-error' data-collection-error role='alert' hidden></p>"
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
        "ai_import_temporary_contention": "save_unavailable",
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
        "invalid_review": (400, "A few details still need attention", "Return to your saved review, check the details, and try again."),
        "authentication_required": (401, "Authentication required", "Sign in to continue."),
        "csrf_denied": (403, "Request rejected", "Reload the page and try again."),
        "authorization_denied": (404, "Page not found", "This page is not available."),
        "not_found": (404, "Page not found", "This page is not available."),
        "invalid_draft": (400, "Draft request unavailable", "Start the import again."),
        "expired_draft": (410, "Review session ended", "Your review session ended, but your saved progress is still available."),
        "expired_checkpoint": (410, "Saved progress expired", "This saved review has reached its 7-day limit. Start a new profile import when you are ready."),
        "stale_review": (409, "Newer progress is available", "This review was updated in another tab or session. Continue from the saved version before making more changes."),
        "unresolved_review": (409, "A few details still need attention", "Return to your saved review and resolve the highlighted details before finding matches."),
        "import_reserved": (409, "Import already in progress", "Finish or cancel the current import before starting another."),
        "checkpoint_available": (409, "Saved progress is available", "Continue your saved review or explicitly discard it before starting another import."),
        "durable_unavailable": (503, "Profile saving unavailable", "AI profile saving is not available in this environment. You can still create your profile manually."),
        "save_unavailable": (503, "We couldn't create your profile just now", "Your progress is saved. Please try again."),
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

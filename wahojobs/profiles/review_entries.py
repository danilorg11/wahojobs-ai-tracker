"""Education editing over the existing entry contract; no inferred associations."""
import base64
import hashlib
import html
import json
import re
from wahojobs.candidate_readability import education_entry_title, EDUCATION_TITLE_SCRIPT
from wahojobs.profiles import work_history_editor

from wahojobs.profiles.education_entries import (
    EDUCATION_ENTRY_KINDS, EDUCATION_ENTRY_STATUSES, MAX_EDUCATION_ENTRIES,
    canonicalize_education_entries_v1, project_education_entries_to_legacy,
)


def explicit_degree_kind(qualification):
    """Recognize an explicitly named degree, never a course or equivalence."""
    value = str(qualification or "").strip()
    if re.search(r"\b(?:exchange|course|studies|equivalent|candidate|not|without)\b", value, re.I):
        return None
    for pattern, kind in ((r"bachelor(?:'s)?\s+(?:of\b|degree\b)", "bachelor"),
                          (r"master(?:'s)?\s+(?:of\b|degree\b)", "master"),
                          (r"(?:doctor of philosophy\b|ph\.?d\.?(?:\s|$))", "doctorate")):
        if re.match(pattern, value, re.I):
            return kind
    return None


def read_education_entries(raw):
    if not raw:
        return None  # Older forms cannot change the represented associations.
    if type(raw) is not str or len(raw) > 24576:
        raise ValueError("Review the education entries.")
    return canonicalize_education_entries_v1(json.loads(raw))


def apply_education_entries(education, raw, *, prior_education=None):
    entries = read_education_entries(raw)
    if entries is not None and prior_education is not None:
        # The V1 review shadow cannot distinguish linked years from independent
        # facts. Use the trusted V2 entries to remove only their old year shadows
        # before projecting the explicitly reviewed replacement entries.
        education = dict(education, graduation_years=unpaired_education(prior_education).get('graduation_years', []))
    return (education if entries is None else
            project_education_entries_to_legacy(entries, education))


def unpaired_education(education):
    """Keep unrelated legacy facts; never zip independent source lists."""
    result = {k: v for k, v in education.items() if k != "entries"}
    entries = education.get("entries", [])
    for name, component in (("degrees", "qualification"), ("fields_or_domains", "field"),
                            ("institutions", "institution"), ("graduation_years", "completion_year")):
        represented = {item[component] for item in entries if item[component]}
        result[name] = [v for v in result.get(name, []) if v not in represented]
    if entries:
        result["education_level"] = "not_specified"
        result["completion_status"] = "unknown"
    return result


def _row(entry, *, template=False):
    fields = []
    for key, label in (("kind", "Education type"), ("qualification", "Degree, course or exchange study"),
                       ("field", "Field of study"), ("institution", "School or institution"),
                       ("status", "Completion status"), ("completion_year", "Completion year (optional)")):
        value = entry.get(key) or ""
        if key in {"kind", "status"}:
            options = EDUCATION_ENTRY_KINDS if key == "kind" else EDUCATION_ENTRY_STATUSES
            labels = {"not_specified": "Not specified / non-degree study", "unknown": "Not specified", "phd": "PhD"}
            control = f"<select data-education-key='{key}'>" + "".join(
                f"<option value='{html.escape(code)}'{' selected' if code == value else ''}>"
                f"{html.escape(labels.get(code, code.replace('_', ' ').title()))}</option>"
                for code in sorted(options)) + "</select>"
        else:
            extra = "inputmode='numeric' pattern='[0-9]{4}' maxlength='4'" if key == "completion_year" else "maxlength='128'"
            control = f"<input data-education-key='{key}' value='{html.escape(str(value), quote=True)}' {extra}>"
        fields.append(f"<label class='review-field'><span>{label}</span>{control}</label>")
    summary = ' · '.join(v for v in (education_entry_title(entry), entry.get('institution')) if v) or 'New education or study'
    return (f"<details data-education-entry{' open' if template else ''}><summary data-education-summary>{html.escape(summary)}</summary><div class='review-grid'>"
            + "".join(fields) + "</div><button type='button' class='button-quiet' data-remove-education>Remove this entry</button></details>")


def education_editor(entries, *, validate=True):
    if validate:
        entries = canonicalize_education_entries_v1(entries)
    empty = dict(kind="not_specified", qualification="", field="", institution="", status="not_specified", completion_year=None)
    # Group explicitly labeled additional studies for presentation only. Keep
    # each original record, institution, status and ordering in the form.
    rows = []
    in_studies = False
    for entry in entries:
        study = bool(re.fullmatch(r'additional stud(?:y|ies)', entry.get('qualification', ''), re.I))
        if study and not in_studies:
            rows.append("<details data-study-group><summary>Additional studies</summary>")
        if in_studies and not study:
            rows.append('</details>')
        in_studies = study
        rows.append(_row(entry))
    if in_studies:
        rows.append('</details>')
    return (f"<div id='education_entries' tabindex='-1' data-education-editor data-limit='{MAX_EDUCATION_ENTRIES}'>"
            f"<input type='hidden' name='education_entries' value='{html.escape(json.dumps(entries), quote=True)}'>"
            "<div data-education-items>" + "".join(rows) + "</div>"
            "<div class='education-add-action'><button type='button' class='button-quiet' data-add-education>Add education or study</button></div>"
            f"<p data-education-limit role='status' hidden>Keep up to {MAX_EDUCATION_ENTRIES} education entries. Your restored details are retained; remove an entry before reviewing changes.</p>"
            f"<template data-education-template>{_row(empty, template=True)}</template></div>")


def employment_records(raw):
    """Keep each record intact, including commas in an employer/date line."""
    from wahojobs.profiles.canonical_v2 import _validate_string_list, MAX_DYNAMIC_LABEL_LENGTH, normalize_comparison_label
    if type(raw) is not str:
        raise ValueError("Review the employment details.")
    values = json.loads(raw) if raw.startswith('[') else ([raw] if raw else [])
    if isinstance(values, list) and all(isinstance(value, str) for value in values):
        values = sorted(values, key=normalize_comparison_label)
    errors = []
    _validate_string_list(values, errors, limit=128, item_length=MAX_DYNAMIC_LABEL_LENGTH)
    if errors:
        raise ValueError("Keep each employment entry within 128 characters.")
    return values


def employment_editor(values):
    return work_history_editor.render(values)


EDUCATION_EDITOR_SCRIPT = """(function(){'use strict';
""" + EDUCATION_TITLE_SCRIPT + """
function educationUndo(scope){
 if(scope._educationUndo)return scope._educationUndo;
 var stack=[],notice=document.createElement('div'),message=document.createElement('span'),undo=document.createElement('button');
 var isEducation=scope.id==='section-education'||scope.hasAttribute('data-education-editor');
 notice.dataset[isEducation?'educationUndo':'sectionUndo']='';notice.hidden=true;message.setAttribute('role','status');message.setAttribute('aria-live','polite');
 undo.type='button';undo.className='button-quiet';undo.dataset[isEducation?'undoEducation':'undoSection']='';undo.textContent='Undo';
 notice.append(message,undo);
 var editor=scope.querySelector('[data-education-editor]');
 if(editor)editor.after(notice);else if(scope.querySelector('[data-section-done]'))scope.querySelector('[data-section-done]').before(notice);else scope.appendChild(notice);
 function show(){var next=stack[stack.length-1];notice.hidden=!next;message.textContent=next?'Removed '+next.label+'.':'';undo.setAttribute('aria-label',next?'Undo removal of '+next.label:'Undo removal');}
 undo.addEventListener('click',function(){var next=stack.pop();if(!next)return;next.restore();show();next.focus();});
 scope._educationUndo={remember:function(item){stack.push(item);show();undo.focus();}};
 return scope._educationUndo;
}
document.querySelectorAll('#section-education,#section-experience,#section-skills').forEach(educationUndo);
""" + work_history_editor.SCRIPT + """
document.querySelectorAll('[data-education-editor]').forEach(function(editor){
var items=editor.querySelector('[data-education-items]'),hidden=editor.querySelector('[name=education_entries]');
var undo=educationUndo(editor.closest('#section-education')||editor);
function entrySummary(item){return [educationEntryTitle(item),item.institution,item.completion_year,item.status&&['not_specified','unknown'].indexOf(item.status)===-1?item.status.replaceAll('_',' '):''].filter(Boolean).join(' · ');}
editor._entrySummary=entrySummary;
function read(row){var item={};row.querySelectorAll('[data-education-key]').forEach(function(input){var value=input.value.trim();item[input.dataset.educationKey]=input.dataset.educationKey==='completion_year'?(value?Number(value):null):value;});return item;}
function populated(item){return item.qualification||item.field||item.institution||item.completion_year||(item.kind&&item.kind!=='not_specified')||(item.status&&['not_specified','unknown'].indexOf(item.status)===-1);}
function sync(){var rows=[];items.querySelectorAll('[data-education-entry]').forEach(function(row){if(row.hidden)return;var item=read(row);row.querySelector('[data-education-summary]').textContent=entrySummary(item)||'New education or study';if(populated(item))rows.push(item);});items.querySelectorAll('[data-study-group]').forEach(function(group){group.hidden=!Array.from(group.querySelectorAll('[data-education-entry]')).some(function(row){return !row.hidden;});});editor.querySelector('[data-education-limit]').hidden=rows.length<=Number(editor.dataset.limit);hidden.value=JSON.stringify(rows);hidden.dispatchEvent(new Event('profile-editor-change',{bubbles:true}));}
editor.addEventListener('input',sync);editor.addEventListener('change',sync);
editor.addEventListener('click',function(event){var remove=event.target.closest('[data-remove-education]');if(remove){var row=remove.closest('[data-education-entry]');if(row.hidden)return;var label=entrySummary(read(row))||'education entry',states=Array.from(row.querySelectorAll('input,select,textarea,button')).map(function(control){return [control,control.disabled];});row.hidden=true;states.forEach(function(pair){pair[0].disabled=true;});sync();undo.remember({label:'education: '+label,restore:function(){row.hidden=false;states.forEach(function(pair){pair[0].disabled=pair[1];});sync();},focus:function(){for(var parent=row;parent;parent=parent.parentElement){if(parent.tagName==='DETAILS')parent.open=true;}(row.querySelector('input:not(:disabled),select:not(:disabled)')||row.querySelector('summary')).focus();}});return;}if(event.target.closest('[data-add-education]')){var rows=Array.from(items.querySelectorAll('[data-education-entry]')).filter(function(row){return !row.hidden;}),empty=rows.find(function(row){return !populated(read(row));});if(empty){empty.open=true;empty.querySelector('input,select').focus();return;}if(rows.length>=Number(editor.dataset.limit))return;items.appendChild(editor.querySelector('template').content.cloneNode(true));sync();items.lastElementChild.querySelector('input,select').focus();}});
});})();"""
EDUCATION_EDITOR_SHA256 = base64.b64encode(hashlib.sha256(EDUCATION_EDITOR_SCRIPT.encode()).digest()).decode()

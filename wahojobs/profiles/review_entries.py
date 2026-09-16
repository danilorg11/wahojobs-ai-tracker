"""Education editing over the existing entry contract; no inferred associations."""
import base64
import hashlib
import html
import json
import re

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


def apply_education_entries(education, raw):
    entries = read_education_entries(raw)
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
            labels = {"not_specified": "Not specified / non-degree study", "unknown": "Not specified"}
            control = f"<select data-education-key='{key}'>" + "".join(
                f"<option value='{html.escape(code)}'{' selected' if code == value else ''}>"
                f"{html.escape(labels.get(code, code.replace('_', ' ').title()))}</option>"
                for code in sorted(options)) + "</select>"
        else:
            extra = "inputmode='numeric' pattern='[0-9]{4}' maxlength='4'" if key == "completion_year" else "maxlength='128'"
            control = f"<input data-education-key='{key}' value='{html.escape(str(value), quote=True)}' {extra}>"
        fields.append(f"<label class='review-field'><span>{label}</span>{control}</label>")
    summary = ' · '.join(str(entry.get(k) or '') for k in ('qualification', 'field', 'institution') if entry.get(k)) or 'New education or study'
    return (f"<details data-education-entry{' open' if template else ''}><summary data-education-summary>{html.escape(summary)}</summary><div class='review-grid'>"
            + "".join(fields) + "</div><button type='button' data-remove-education>Remove this entry</button></details>")


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
            "<button type='button' data-add-education>Add education or study</button>"
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
    def row(value):
        return ("<div data-employment-entry><label class='review-field'><span>Role, employer and dates</span>"
                f"<textarea rows='2' maxlength='128' data-employment-value>{html.escape(value)}</textarea></label>"
                "<button type='button' data-remove-employment>Remove employment detail</button></div>")
    return ("<div data-employment-editor>"
            f"<input type='hidden' name='recent_roles' value='{html.escape(json.dumps(values), quote=True)}'>"
            "<div data-employment-items>" + ''.join(row(v) for v in values) + "</div>"
            "<button type='button' data-add-employment>Add employment detail</button>"
            f"<template>{row('')}</template></div>")


EDUCATION_EDITOR_SCRIPT = """(function(){'use strict';
function removeWithUndo(row,selector,sync){var states=Array.from(row.querySelectorAll('input,select,textarea,button')).map(function(control){return [control,control.disabled];});row.hidden=true;states.forEach(function(pair){pair[0].disabled=true;});var undo=document.createElement('button');undo.type='button';undo.className='button-quiet';undo.dataset.undoEntry='';undo.textContent='Undo removal';row.after(undo);sync();undo.addEventListener('click',function(){row.hidden=false;states.forEach(function(pair){pair[0].disabled=pair[1];});sync();undo.remove();row.querySelector(selector).focus();});undo.focus();}
document.querySelectorAll('[data-employment-editor]').forEach(function(editor){
var items=editor.querySelector('[data-employment-items]'),hidden=editor.querySelector('[name=recent_roles]');
function sync(){hidden.value=JSON.stringify(Array.from(items.querySelectorAll('[data-employment-value]')).filter(function(input){return !input.closest('[data-employment-entry]').hidden;}).map(function(input){return input.value.trim();}).filter(Boolean));hidden.dispatchEvent(new Event('profile-editor-change',{bubbles:true}));}
editor.addEventListener('input',sync);
editor.addEventListener('click',function(event){var remove=event.target.closest('[data-remove-employment]');if(remove){removeWithUndo(remove.closest('[data-employment-entry]'),'[data-employment-value]',sync);return;}if(event.target.closest('[data-add-employment]')){var rows=Array.from(items.querySelectorAll('[data-employment-entry]')).filter(function(row){return !row.hidden;}),empty=rows.find(function(row){return !row.querySelector('[data-employment-value]').value.trim();});if(empty){empty.querySelector('[data-employment-value]').focus();return;}if(rows.length>=128)return;items.appendChild(editor.querySelector('template').content.cloneNode(true));sync();items.lastElementChild.querySelector('[data-employment-value]').focus();}});
});
document.querySelectorAll('[data-education-editor]').forEach(function(editor){
var items=editor.querySelector('[data-education-items]'),hidden=editor.querySelector('[name=education_entries]');
function read(row){var item={};row.querySelectorAll('[data-education-key]').forEach(function(input){var value=input.value.trim();item[input.dataset.educationKey]=input.dataset.educationKey==='completion_year'?(value?Number(value):null):value;});return item;}
function populated(item){return item.qualification||item.field||item.institution||item.completion_year||(item.kind&&item.kind!=='not_specified')||(item.status&&['not_specified','unknown'].indexOf(item.status)===-1);}
function sync(){var rows=[];items.querySelectorAll('[data-education-entry]').forEach(function(row){if(row.hidden)return;var item=read(row);row.querySelector('[data-education-summary]').textContent=[item.qualification,item.field,item.institution,item.status&&['not_specified','unknown'].indexOf(item.status)===-1?item.status.replaceAll('_',' '):''].filter(Boolean).join(' · ')||'New education or study';if(populated(item))rows.push(item);});hidden.value=JSON.stringify(rows);hidden.dispatchEvent(new Event('profile-editor-change',{bubbles:true}));}
editor.addEventListener('input',sync);editor.addEventListener('change',sync);
editor.addEventListener('click',function(event){var remove=event.target.closest('[data-remove-education]');if(remove){removeWithUndo(remove.closest('[data-education-entry]'),'input,select',sync);return;}if(event.target.closest('[data-add-education]')){var rows=Array.from(items.querySelectorAll('[data-education-entry]')).filter(function(row){return !row.hidden;}),empty=rows.find(function(row){return !populated(read(row));});if(empty){empty.open=true;empty.querySelector('input,select').focus();return;}if(rows.length>=Number(editor.dataset.limit))return;items.appendChild(editor.querySelector('template').content.cloneNode(true));sync();items.lastElementChild.querySelector('input,select').focus();}});
});})();"""
EDUCATION_EDITOR_SHA256 = base64.b64encode(hashlib.sha256(EDUCATION_EDITOR_SCRIPT.encode()).digest()).decode()

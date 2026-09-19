"""Guided work-history controls over the existing lossless string contract."""
from html import escape
import json
import re


def parts(value):
    # Only reopen our explicitly labelled format. Never guess associations in
    # older free text, infer employment dates, or manufacture experience years.
    match = re.fullmatch(r'Title: ([^|]+?)(?: \| At: ([^|]+?))?(?: \| From: ([0-9]{4}))?(?: \| To: ([0-9]{4}|Present))?', value)
    return match.groups() if match else None


def summary(value):
    fields = parts(value)
    if not fields:
        return value
    title, organization, start, end = fields
    period = f'{start}–{end}' if start and end else f'Started {start}' if start else f'Until {end}' if end and end != 'Present' else end
    return ' · '.join(v for v in (title, organization, period) if v)


def render(values):
    def row(value):
        parsed = parts(value) if value else ('', '', '', '')
        if parsed:
            title, organization, start, end = (v or '' for v in parsed)
            content = ''
            for key, label, val, example in (
                ('title', 'Job title or freelance role', title, 'Customer support representative'),
                ('organization', 'Company or client (optional)', organization, 'Acme, or Self-employed'),
                ('start', 'Start year (optional)', start, '2021'),
                ('end', 'End year (optional)', '' if end == 'Present' else end, '2024')):
                attrs = "inputmode='numeric' pattern='[0-9]{4}' maxlength='4'" if key in ('start', 'end') else "maxlength='128' pattern='[^|]*'"
                content += ("<label class='review-field'" + (" hidden" if key == 'end' and end == 'Present' else '')
                            + f"><span>{label}</span><input data-work-key='{key}' value='{escape(val, quote=True)}' placeholder='{example}' {attrs}"
                            + (" disabled" if key == 'end' and end == 'Present' else '') + '></label>')
            content = "<div class='review-grid'>" + content + '</div>'
            content += ("<label class='review-checkbox'><input type='checkbox' data-work-current"
                        + (' checked' if end == 'Present' else '') + ">I currently work here</label>"
                        "<p class='field-help'>Years are enough. Leave the period blank if you do not remember it.</p>"
                        "<p data-work-error role='status' hidden></p>"
                        f"<input type='hidden' data-employment-value value='{escape(value, quote=True)}'>")
        else:
            content = ("<label class='review-field'><span>Saved work description</span>"
                       f"<textarea rows='2' maxlength='128' data-employment-value>{escape(value)}</textarea></label>"
                       "<p class='field-help'>This is your existing description. Keep or edit it as written; no dates or employer have been guessed.</p>")
        return ("<div class='correction-item' data-employment-entry>" + content
                + "<button type='button' class='button-quiet' data-remove-employment>Remove this job</button></div>")
    return ("<div id='work-history' tabindex='-1' data-employment-editor>"
            f"<input type='hidden' name='recent_roles' value='{escape(json.dumps(values, ensure_ascii=False, separators=(',', ':')), quote=True)}'>"
            "<div data-employment-items>" + ''.join(row(v) for v in values) + '</div>'
            "<div class='education-add-action'><button type='button' class='button-quiet secondary-add' data-add-employment>Add a job</button></div>"
            f"<template>{row('')}</template></div>")


SCRIPT = r"""
document.querySelectorAll('[data-employment-editor]').forEach(function(editor){
var items=editor.querySelector('[data-employment-items]'),hidden=editor.querySelector('[name=recent_roles]');
editor._summary=function(value){var m=value.match(/^Title: ([^|]+?)(?: \| At: ([^|]+?))?(?: \| From: ([0-9]{4}))?(?: \| To: ([0-9]{4}|Present))?$/);if(!m)return value;var period=m[3]&&m[4]?m[3]+'–'+m[4]:m[3]?'Started '+m[3]:m[4]&&m[4]!=='Present'?'Until '+m[4]:m[4];return [m[1],m[2],period].filter(Boolean).join(' · ');};
var undo=educationUndo(editor.closest('#section-experience')||editor);
function controls(row){return Array.from(row.querySelectorAll('[data-work-key]'));}
function read(row){var fields=controls(row);if(!fields.length)return row.querySelector('[data-employment-value]').value.trim();
 var values={};fields.forEach(function(input){values[input.dataset.workKey]=input.value.trim();});
 var current=row.querySelector('[data-work-current]').checked,end=fields[3];end.disabled=current;end.closest('label').hidden=current;values.end=current?'Present':values.end;
 var populated=Object.values(values).some(Boolean),title=fields[0],error='';
 if(populated&&!values.title)error='Add a job title, or remove this empty job.';
 else if(values.start&&values.end&&values.end!=='Present'&&Number(values.end)<Number(values.start))error='The end year must be the same as or later than the start year.';
 var line=populated?'Title: '+values.title:'';
 [['organization','At'],['start','From'],['end','To']].forEach(function(pair){if(values[pair[0]])line+=' | '+pair[1]+': '+values[pair[0]];});
 if(line.length>128)error='Shorten the job title or company name by '+(line.length-128)+' characters.';
 title.setCustomValidity(error);var note=row.querySelector('[data-work-error]');note.textContent=error;note.hidden=!error;
 row.querySelector('[data-employment-value]').value=line;return line;
}
function sync(){hidden.value=JSON.stringify(Array.from(items.querySelectorAll('[data-employment-entry]')).filter(function(row){return !row.hidden;}).map(read).filter(Boolean));hidden.dispatchEvent(new Event('profile-editor-change',{bubbles:true}));}
function first(row){return row.querySelector('[data-work-key],textarea');}
editor.addEventListener('input',sync);editor.addEventListener('change',sync);
editor.addEventListener('click',function(event){var remove=event.target.closest('[data-remove-employment]');
 if(remove){var row=remove.closest('[data-employment-entry]'),label=editor._summary(read(row))||'empty job',states=Array.from(row.querySelectorAll('input,textarea,button')).map(function(input){return [input,input.disabled];});row.hidden=true;states.forEach(function(pair){pair[0].disabled=true;});sync();undo.remember({label:label,restore:function(){row.hidden=false;states.forEach(function(pair){pair[0].disabled=pair[1];});sync();},focus:function(){for(var parent=row.parentElement;parent;parent=parent.parentElement){if(parent.tagName==='DETAILS')parent.open=true;}first(row).focus();}});return;}
 if(event.target.closest('[data-add-employment]')){var rows=Array.from(items.querySelectorAll('[data-employment-entry]')).filter(function(row){return !row.hidden;}),empty=rows.find(function(row){return !read(row);});if(empty){first(empty).focus();return;}if(rows.length>=128)return;items.appendChild(editor.querySelector('template').content.cloneNode(true));sync();first(items.lastElementChild).focus();}
});
});
"""

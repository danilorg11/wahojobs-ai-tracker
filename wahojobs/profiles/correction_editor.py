"""Candidate presentation for the existing authenticated correction contract.

No profile persistence or interpretation lives here. The complete existing form
contract is retained, including values which are not candidate-facing controls.
"""
import base64
import hashlib
import html
import json

from wahojobs.profiles.review_entries import (
    EDUCATION_EDITOR_SCRIPT, education_editor, employment_editor, unpaired_education,
)
from wahojobs.profiles.canonical import EDUCATION_COMPLETION_STATUSES
from wahojobs.profiles import item_experience_editor, domain_duration_editor


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _chips(name, values, label):
    # Correction rows use the existing CSV authority, with their own controls.
    # Intake controls have a different removal/autosave lifecycle.
    singular = {'job_titles': 'Role', 'specialties': 'Activity', 'skills': 'Skill',
        'software_tools': 'Software or tool', 'degrees': 'Qualification',
        'education_fields': 'Study topic', 'institutions': 'School',
        'technical_skills': 'Technical skill', 'writing_research_skills': 'Writing or research skill',
        'administrative_support_skills': 'Administration or support skill',
        'domain_specific_skills': 'Specialist skill'}[name]
    def item(value, index):
        return ("<div class='correction-item' data-collection-item>"
            "<div class='correction-item-main'><label class='review-field'>"
            f"<span>{esc(singular)}</span><input value='{esc(value)}'></label>"
            "<input type='checkbox' data-collection-remove hidden>"
            f"<button type='button' class='button-quiet' data-collection-remove-action aria-label='Remove {esc(singular.lower())}'>Remove</button>"
            "</div></div>")
    return (f"<div class='review-collection skills-collection' id='{esc(name)}' tabindex='-1' data-chips='{esc(name)}'>"
            f"<h3>{esc(label)}</h3><div data-chip-items class='expertise-compact-list'>"
            + "".join(item(v, i) for i, v in enumerate(values)) + "</div>"
            f"<button type='button' class='button-quiet' data-add-chip>Add {esc(singular.lower())}</button>"
            f"<template>{item('', 'new')}</template></div>")


def render_editor(support, canonical, run_id, token, *, action, back_url,
                  education, form_defaults, focus=None, submitted=None, issue=None, cancel_url='/account/profile', item_details=(), manual_draft=False, preference_model=None):
    fields = dict(form_defaults)
    if not manual_draft:
        fields.pop("profile_draft_fingerprint", None)
    fields.pop("credentials_confirmed", None)
    unpaired = unpaired_education(education)
    for name, key in (("education_level", "education_level"), ("degrees", "degrees"),
                      ("education_fields", "fields_or_domains"), ("institutions", "institutions"),
                      ("education_status", "completion_status")):
        value = unpaired.get(key)
        fields[name] = support.review_csv(value) if isinstance(value, list) else value or ""
    fields["education_entries"] = '' if manual_draft else json.dumps(education.get("entries", []))
    fields['item_experience'] = json.dumps(list(item_details))
    # Match the established legacy form projection. The correction service
    # preserves an untouched legacy level from the authoritative revision.
    if fields['education_level'] not in support.EDUCATION_LEVELS:
        fields['education_level'] = support.EDUCATION_LEVELS[0]
    if submitted:
        for name in fields.keys() | support.PROFILE_REVIEW_CHECKBOX_FIELDS:
            if name in submitted and len(submitted[name]) == 1:
                fields[name] = submitted[name][0]
            elif name in support.PROFILE_REVIEW_CHECKBOX_FIELDS:
                fields.pop(name, None)
    rendered = set()

    def text(name, label, *, area=False):
        rendered.add(name)
        value = fields.get(name)
        if name == 'work_authorization' and value == 'unknown':
            value = ''
        error = f" aria-invalid='true' aria-describedby='correction-error'" if issue and issue[0] == name else ""
        control = (f"<textarea id='{name}' name='{name}' rows='2'{error}>{esc(value)}</textarea>" if area
                   else f"<input id='{name}' name='{name}' value='{esc(value)}'{error}>")
        return f"<label class='review-field'><span>{esc(label)}</span>{control}</label>"

    def select(name, label, options):
        rendered.add(name)
        current = fields.get(name, "")
        if isinstance(options, (tuple, list, set, frozenset)):
            options = {v: support.profile_review_option_label(v) for v in sorted(options)}
        options = dict(options)
        if current not in options:
            options[current] = current.replace('_', ' ').capitalize() if current else "Not specified"
        return (f"<label class='review-field'><span>{esc(label)}</span><select id='{name}' name='{name}'>"
                + ''.join(f"<option value='{esc(v)}'{' selected' if v == current else ''}>{esc(t)}</option>"
                          for v, t in options.items()) + "</select></label>")

    def chips(name, values, label):
        if submitted:
            values = support.canonical_review.string_list(fields.get(name, ""))
        return _chips(name, values or [], label)

    def check(name, label):
        rendered.add(name)
        return support.review_checkbox(name, label, fields.get(name) == '1')

    def choices(name, label, options):
        selected = support.canonical_review.string_list(fields.get(name, ''))
        return (f"<fieldset class='choice-fieldset' data-choices='{name}'><legend>{esc(label)}</legend><div class='choice-grid'>"
            + ''.join(f"<label class='choice-card'><input type='checkbox' value='{esc(value)}'{' checked' if value in selected else ''}><span>{esc(support.profile_review_option_label(value))}</span></label>" for value in sorted(set(options) | set(selected)))
            + '</div></fieldset>')

    def section(name, label, summary, content, *, opened=False):
        return (f"<details class='candidate-section' id='section-{name}'{' open' if opened else ''}>"
                f"<summary><strong>{esc(label)}</strong><span data-section-summary>{esc(summary or 'Optional — add details if useful')}</span>"
                "<span class='section-edit'>Edit</span></summary><div class='candidate-section-body'>"
                + content + "<button type='button' class='button-quiet' data-section-done>Done editing</button></div></details>")

    location = section("location", "Location", ', '.join(fields[k] for k in ('city', 'region', 'country') if fields.get(k)),
        text('country', 'Country where you currently live') + "<div class='review-grid'>"
        + text('region', 'Region or state (optional)') + text('city', 'City (optional)') + "</div>", opened=True)
    language_rows = []
    for i in range(support.profile_review_language_slots(canonical)):
        content = ("<div class='review-grid'>" + text(f'language_{i}', 'Language')
            + select(f'language_proficiency_{i}', 'Proficiency', support.LANGUAGE_PROFICIENCIES) + "</div>"
            + "<details><summary>Language variety (optional)</summary><p>For example, Brazilian Portuguese.</p>"
            + text(f'language_locale_{i}', 'Variety or region') + "</details>")
        language_rows.append(f"<div class='candidate-language' data-language-row{' hidden' if not fields.get(f'language_{i}') else ''}>" + content
            + "<button type='button' class='button-quiet' data-remove-language>Remove language</button></div>")
    language_summary = ', '.join(
        f"{fields[f'language_{i}']} ({support.profile_review_option_label(fields[f'language_proficiency_{i}'])})"
        + (f" — {fields[f'language_locale_{i}']}" if fields.get(f'language_locale_{i}') else '')
        for i in range(support.profile_review_language_slots(canonical)) if fields.get(f'language_{i}'))
    languages = section('languages', 'Languages', language_summary, ''.join(language_rows)
        + "<div data-language-undo></div><button type='button' class='button-quiet' data-add-language>Add a language</button>"
        + "<p data-language-limit hidden>All language fields are in use. Remove an entry to add another.</p>", opened=focus == 'languages')
    experience = canonical.get('experience', {})
    domain_years = experience.get('years_by_domain', {})
    if isinstance(domain_years, list):
        domain_years = {item['domain']: item['years'] for item in domain_years}
    domain_duration = domain_duration_editor.render(domain_years, fields.get('domain_years_review', ''))
    rendered.add('recent_roles')
    roles = json.loads(fields['recent_roles'])
    employment = section('experience', 'Experience', ' · '.join(experience.get('job_titles', [])),
        chips('job_titles', experience.get('job_titles'), 'Roles') + employment_editor(roles)
        + chips('specialties', experience.get('specialties'), 'Activities')
        + domain_duration
        + "<details><summary>Experience length (optional)</summary>" + text('total_years', 'Total years of work experience')
        + check('no_experience', 'I have no prior work experience') + "<p>Total career years do not establish years in a particular profession.</p></details>", opened=focus == 'experience')
    rendered.add('education_entries')
    entries = None
    try:
        entries = json.loads(fields['education_entries'])
        education_content = education_editor(entries)
    except (ValueError, TypeError):
        # Display invalid-but-shaped draft rows in the same editor. Never expose
        # serialized profile JSON as a candidate correction mechanism.
        if isinstance(entries, list) and all(isinstance(e, dict) for e in entries):
            education_content = education_editor(entries, validate=False)
        else:
            rendered.discard('education_entries')
            education_content = "<p id='education_entries' tabindex='-1'>These education edits could not be read. The submitted draft has been retained.</p>"
    legacy = (chips('degrees', unpaired.get('degrees'), 'Other qualifications')
        + chips('education_fields', unpaired.get('fields_or_domains'), 'Other study topics')
        + chips('institutions', unpaired.get('institutions'), 'Other schools')
        + select('education_level', 'Education level (if not listed above)', support.EDUCATION_LEVELS)
        + select('education_status', 'Study status', EDUCATION_COMPLETION_STATUSES)
        + check('no_degree', 'I have no university degree'))
    if manual_draft:
        rendered.add('education_entries')
        # Creation uses the established V1 independent fields. Do not expose
        # linked V2 study records or item experience that this route cannot store.
        education_content = "<input type='hidden' name='education_entries' value=''>"
        education_body = (education_content + '<p>These are independent details. No relationship between qualifications, study topics and schools is assumed.</p>'
                          + legacy.replace('Other qualifications', 'Qualifications').replace('Other study topics', 'Study topics').replace('Other schools', 'Schools'))
    else:
        education_body = education_content + "<details><summary>Other education details</summary>" + legacy + "</details>"
    education_section = section('education', 'Education and studies', f"{len(education.get('entries', []))} entries" if education.get('entries') else support.review_csv(canonical.get('education', {}).get('degrees')),
        education_body, opened=focus == 'education')
    skills = canonical.get('skills', {})
    skill_content = chips('skills', skills.get('normalized'), 'Skills') + chips('software_tools', skills.get('software_tools'), 'Software and tools')
    extra = ''.join(chips(name, skills.get(key), label) for name, key, label in (
        ('technical_skills', 'technical', 'Technical skills'), ('writing_research_skills', 'writing_research', 'Writing and research'),
        ('administrative_support_skills', 'administrative_support', 'Administration and support'), ('domain_specific_skills', 'domain_specific', 'Specialist skills')) if skills.get(key))
    skill_section = section('skills', 'Skills and tools', support.review_csv(skills.get('normalized') or skills.get('software_tools')),
        skill_content + ('<details open>' if focus in ('technical_skills', 'domain_specific_skills') else '<details>')
        + '<summary>Additional skills</summary>' + extra + '</details>',
        opened=focus in ('skills', 'software_tools', 'technical_skills', 'domain_specific_skills'))
    rendered.add('flexible')
    preferences = section('preferences', 'Work preferences', 'Optional',
        check('remote', 'I prefer remote work')
        + select('availability', 'Workload / timing preference', {'unknown':'Not specified','immediate':'Prefer an immediate start','available':'Open to work','limited':'Prefer limited hours','unavailable':'Not currently looking','full-time':'Full-time','part-time':'Part-time'})
        + "<p class='field-help'>Workload choices are preferences, not a ban on other schedules or a statement of available hours. "
          "For a firm restriction, use “part-time only” or “full-time only” in Firm constraints below.</p>"
        + support.review_checkbox('flexible', 'I prefer flexible hours', fields.get('flexible') == '1')
        + select('synchronous_preference', 'Meetings and scheduled collaboration', support.canonical_review.SYNCHRONOUS_PREFERENCES)
        + select('phone_preference', 'Phone work', support.canonical_review.PHONE_PREFERENCES)
        + '<details><summary>Schedule and contract preferences</summary>'
        + choices('schedule', 'Schedule preferences', support.canonical_review.SCHEDULE_PREFERENCES)
        + choices('employment_types', 'Workload and contract preferences', support.canonical_review.EMPLOYMENT_TYPES) + '</details>', opened=focus == 'preferences')
    if preference_model is not None:
        from wahojobs.profiles.preference_presentation import render_preference_editor, preference_summary
        # The typed authority is editable; its legacy mirrors travel unchanged
        # and are derived again on the server after the candidate's review.
        rendered.difference_update({'availability', 'flexible', 'synchronous_preference', 'phone_preference'})
        preferences = section('preferences', 'Work preferences',
            ' · '.join(preference_summary({'preference_model': preference_model})),
            check('remote', 'I prefer remote work') + render_preference_editor(preference_model, submitted)
            + "<p class='field-help'>For a firm workload restriction, use “part-time only” or “full-time only” in Firm constraints below.</p>",
            opened=focus == 'preferences' or bool(issue and issue[0] == 'section-preferences'))
    optional = section('optional', 'Permissions, licenses and constraints', 'Optional',
        text('work_authorization', 'Work permission or permit (optional)')
        + text('eligible_countries', 'Countries where you have permission to work (optional)')
        + text('geographic_restrictions', 'Location constraints (optional)')
        + '<p class="field-help">These describe your permissions, not an employer’s eligibility rules.</p>'
        + select('credential_status', 'Professional credentials', support.CREDENTIAL_STATUSES)
        + ''.join(text(n,l,area=True) for n,l in (
            ('certifications','Certifications'),('licenses','Professional licenses'),('jurisdictions','License jurisdictions'),
            ('security_clearances','Security clearances'),('hard_constraints','Firm constraints'),
            ('accessibility_constraints','Accessibility or working needs'),('soft_preferences','Other preferences')))
        + check('no_specialized_credentials', 'I have no specialized credentials'))
    # Required legacy fields and internal taxonomy are carried unchanged. Their
    # absence from the visible controls never implies removal or new evidence.
    hidden = ''.join(f"<input type='hidden' name='{esc(k)}' value='{esc(v)}'>" for k,v in fields.items() if k not in rendered and k != 'credentials_confirmed')
    feedback = (f"<div role='alert' id='correction-error'><a href='#{esc(issue[0])}'>{esc(issue[1])}</a></div>" if issue else '')
    focus_target = {'education': 'degrees', 'skills': 'skills', 'technical_skills': 'technical_skills',
                    'domain_specific_skills': 'domain_specific_skills', 'software_tools': 'software_tools', 'languages': 'language_0',
                    'experience': 'job_titles', 'preferences': 'availability', 'location': 'country'}.get(focus, '')
    if focus == 'preferences' and preference_model is not None:
        focus_target = 'beta_preference_workloads'
    return (f"<form method='post' action='{esc(action)}' class='profile-review-form candidate-correction' id='profile-review-form' data-focus='{focus_target}'>"
        + hidden + feedback + ("<p>Add the details you want to use for matching. Optional details can stay blank.</p>" if manual_draft else "<p>Edit any section that needs a correction. Optional details can stay blank.</p>")
        + ("<p class='field-help'>Changes are saved to an unconfirmed draft while you edit. Done editing closes a section. Review changes checks the complete profile before confirmation.</p>" if manual_draft else "<p class='field-help'>Done editing closes a section. Use Review changes to save your draft, then confirm the complete profile on the next screen.</p>")
        + "<p data-local-edit-status role='status' aria-live='polite'></p>"
        + "<a class='primary-link' href='#review-actions'>Continue to review</a>"
        + location + languages + employment + education_section + skill_section + preferences + optional
        + "<div class='review-checks'>" + support.review_checkbox('credentials_confirmed',
            'The license and certification information I reviewed is accurate.', fields.get('credentials_confirmed') == '1', required=True) + "</div>"
        + f"<div class='review-actions' id='review-actions'><a data-correction-back href='{esc(back_url)}'>Back</a>"
        + f"<a href='{esc(cancel_url)}'>Cancel</a><button type='submit' id='confirm-profile-button'>Review changes</button></div>"
        + "<p class='field-help'>You will confirm the complete profile on the next screen.</p>" + ('' if manual_draft else item_experience_editor.dialog()) + '</form>'
        + '<style>' + EDITOR_STYLE + item_experience_editor.STYLE + '</style><script>' + EDITOR_SCRIPT + '</script>')


EDITOR_STYLE = """
.candidate-correction {margin-top:16px; max-width:100%;}
.candidate-correction * {min-width:0;}
.candidate-correction .candidate-section {border:1px solid #dce2df;border-radius:10px;margin:12px 0;}
.candidate-section > summary {display:grid;grid-template-columns:1fr auto;gap:5px;padding:16px;cursor:pointer;}
.candidate-section > summary strong {font-size:18px;}
.candidate-section > summary [data-section-summary] {grid-column:1;line-height:1.5;color:#53605b;overflow-wrap:anywhere;}
.candidate-section .section-edit {grid-column:2;grid-row:1;color:#174d3b;text-decoration:underline;}
.candidate-section-body {padding:0 16px 16px;}
.candidate-correction .review-grid {grid-template-columns:repeat(2,minmax(0,1fr));}
.candidate-correction input,.candidate-correction select,.candidate-correction textarea {font:inherit;min-height:44px;max-width:100%;}
.candidate-correction textarea {width:100%;resize:vertical;padding:10px;border:1px solid #aebbb5;border-radius:6px;}
.candidate-correction .review-checkbox {display:flex;align-items:flex-start;gap:10px;line-height:1.5;}
.candidate-correction .review-checkbox input {flex:0 0 18px;width:18px;height:18px;min-height:18px;margin-top:3px;}
.candidate-correction .choice-card input {min-height:18px;height:18px;width:18px;flex:0 0 18px;}
.candidate-correction .review-actions {position:static;align-items:center;gap:20px;margin:24px 0 12px;scroll-margin:20px;}
.candidate-correction button,.candidate-correction a,.candidate-correction summary {min-height:44px;}
.candidate-correction :focus-visible {outline:3px solid #6fa68f;outline-offset:3px;}
.candidate-correction .skills-collection {border:0;padding:0;margin:16px 0;}
.candidate-correction [hidden] {display:none!important;}
.candidate-correction .expertise-compact-list {display:grid;gap:12px;}
.candidate-correction .correction-item {border:1px solid #dce2df;border-radius:8px;padding:12px;}
.candidate-correction .correction-item-main {display:flex;align-items:flex-end;gap:12px;}
.candidate-correction .correction-item-main .review-field {flex:1;margin:0;}
.candidate-correction .correction-item-main input {width:100%;box-sizing:border-box;}
.candidate-correction [data-undo-item],.candidate-correction [data-undo-language] {margin:8px 0;}
.candidate-correction [data-education-entry] {border:1px solid #dce2df;border-radius:8px;margin:8px 0;padding:0 12px;}
.candidate-correction [data-education-entry] > summary {display:block;overflow-wrap:anywhere;line-height:1.5;cursor:pointer;}
.candidate-correction [data-study-group] {margin:16px 0;}
.candidate-correction [data-study-group] > summary {font-weight:700;}
.candidate-correction [data-employment-entry] {margin:12px 0;}
.candidate-correction .candidate-language {border-bottom:1px solid #e8ecea;padding:10px 0;}
.candidate-correction #correction-error {padding:14px;border:1px solid #ad5145;margin:12px 0;}
@media(max-width:700px){.candidate-correction{padding:16px;}.candidate-correction .review-grid{grid-template-columns:1fr;}.candidate-section-body{padding:0 12px 12px;}.candidate-correction .review-actions{gap:16px;}.candidate-correction .review-actions button{width:100%;}.candidate-correction .correction-item-main{flex-wrap:wrap;}.candidate-correction .correction-item-main .review-field{flex-basis:100%;}}
"""

EDITOR_SCRIPT = EDUCATION_EDITOR_SCRIPT + item_experience_editor.SCRIPT + domain_duration_editor.SCRIPT + """
(function(){'use strict';var form=document.querySelector('.candidate-correction');if(!form)return;
var originalFields=JSON.stringify(Array.from(new FormData(form).entries())),submitting=false;
function dirty(){return JSON.stringify(Array.from(new FormData(form).entries()))!==originalFields;}
form.querySelector('[data-correction-back]').addEventListener('click',function(event){if(!form.hasAttribute('data-manual-draft')&&dirty()){event.preventDefault();form.requestSubmit(form.querySelector('button[type=submit]'));}});
window.addEventListener('beforeunload',function(event){if(!form.hasAttribute('data-manual-draft')&&!submitting&&dirty()){event.preventDefault();event.returnValue='';}});
form.querySelectorAll('[data-chips]').forEach(function(group){
 var hidden=form.querySelector('[name="'+group.dataset.chips+'"]'),items=group.querySelector('[data-chip-items]');
 function sync(){hidden.value=Array.from(items.querySelectorAll('[data-collection-item]')).filter(function(item){return !item.querySelector('[data-collection-remove]').checked;}).map(function(item){return item.querySelector('input:not([type=checkbox])').value.trim();}).filter(Boolean).join(', ');}
 group.addEventListener('input',sync);group.addEventListener('change',function(e){if(e.target.matches('[data-collection-remove]'))e.target.closest('[data-collection-item]').hidden=e.target.checked;sync();});
 group.addEventListener('click',function(e){var button=e.target.closest('[data-collection-remove-action]');if(!button)return;var item=button.closest('[data-collection-item]'),removed=item.querySelector('[data-collection-remove]'),input=item.querySelector('input:not([type=checkbox])');removed.checked=true;removed.dispatchEvent(new Event('change',{bubbles:true}));var undo=document.createElement('button');undo.type='button';undo.className='button-quiet';undo.dataset.undoItem='';undo.textContent='Undo removal: '+(input.value.trim()||input.closest('label').textContent.trim());item.after(undo);undo.addEventListener('click',function(){removed.checked=false;removed.dispatchEvent(new Event('change',{bubbles:true}));undo.remove();input.focus();});undo.focus();});
 group.querySelector('[data-add-chip]').addEventListener('click',function(){var empty=Array.from(items.querySelectorAll('[data-collection-item]')).find(function(item){return !item.hidden&&!item.querySelector('input:not([type=checkbox])').value.trim();});if(empty){empty.querySelector('input:not([type=checkbox])').focus();return;}var next=group.querySelector('template').content.cloneNode(true);items.appendChild(next);items.lastElementChild.querySelector('input:not([type=checkbox])').focus();});
});
form.querySelectorAll('[data-choices]').forEach(function(group){group.addEventListener('change',function(){form.querySelector('[name="'+group.dataset.choices+'"]').value=Array.from(group.querySelectorAll('input:checked')).map(function(i){return i.value;}).join(', ');});});
var languageRows=Array.from(form.querySelectorAll('[data-language-row]')),addLanguage=form.querySelector('[data-add-language]');
function languageControls(row){return Array.from(row.querySelectorAll('input,select'));}
function languageCapacity(){var full=languageRows.every(function(row){return !row.hidden;});addLanguage.disabled=full;form.querySelector('[data-language-limit]').hidden=!full;}
languageRows.forEach(function(row){row.querySelector('[data-remove-language]').addEventListener('click',function(){var controls=languageControls(row),previous=controls.map(function(c){return c.value;});controls.forEach(function(c){c.value=c.tagName==='SELECT'?'unspecified':'';});row.hidden=true;var undo=document.createElement('button');undo.type='button';undo.className='button-quiet';undo.dataset.undoLanguage='';undo.textContent='Undo removal: '+(previous[0]||'language');row._undo=undo;form.querySelector('[data-language-undo]').appendChild(undo);undo.addEventListener('click',function(){controls.forEach(function(c,i){c.value=previous[i];});row.hidden=false;row._undo=null;undo.remove();languageCapacity();controls[0].dispatchEvent(new Event('input',{bubbles:true}));controls[0].focus();});languageCapacity();controls[0].dispatchEvent(new Event('input',{bubbles:true}));undo.focus();});});
addLanguage.addEventListener('click',function(){var empty=languageRows.find(function(row){return !row.hidden&&!languageControls(row)[0].value.trim();});var row=empty||languageRows.find(function(r){return r.hidden&&!r._undo;})||languageRows.find(function(r){return r.hidden;});if(!row)return;if(row._undo){row._undo.remove();row._undo=null;}row.hidden=false;languageCapacity();languageControls(row)[0].focus();});languageCapacity();
function value(name){var c=form.querySelector('[name="'+name+'"]');return c&&!(c.type==='checkbox'&&!c.checked)?c.value.trim():'';}
function meaningful(v){return v&&['unknown','unspecified','not_specified'].indexOf(v)===-1;}
function option(name){var c=form.querySelector('[name="'+name+'"]');return c&&meaningful(c.value)?c.selectedOptions[0].textContent:'';}
function jsonValues(name){try{return JSON.parse(value(name)||'[]');}catch(e){return [];}}
function summarize(section){var id=section.id.replace('section-',''),parts=[];
 if(id==='languages')parts=languageRows.map(function(row){var c=languageControls(row);return c[0].value.trim()?c[0].value.trim()+' ('+c[1].selectedOptions[0].textContent+')'+(c[2].value.trim()?' — '+c[2].value.trim():''):'';});
 else if(id==='location')parts=['city','region','country'].map(value);
 else if(id==='experience'){parts=['job_titles','specialties'].map(value).concat(jsonValues('recent_roles'));if(value('total_years'))parts.push('Total career experience: '+value('total_years')+' years');section.querySelectorAll('[data-domain-duration-row]').forEach(function(row){if(!row.hidden)parts.push('Experience in '+row.dataset.domain+': '+(row.querySelector('input').value.trim()||'needs review')+' years');});}
 else if(id==='skills'){var seenSkills=new Set();Array.from(section.querySelectorAll('[data-chips]')).forEach(function(g){value(g.dataset.chips).split(',').forEach(function(v){v=v.trim();var key=v.toLocaleLowerCase();if(v&&!seenSkills.has(key)){seenSkills.add(key);parts.push(v);}});});}
 else if(id==='education'){parts=jsonValues('education_entries').map(function(e){return [e.qualification,e.field,e.institution,e.completion_year,e.status&&e.status!=='unknown'?e.status.replaceAll('_',' '):''].filter(Boolean).join(', ');});parts=parts.concat(['degrees','education_fields','institutions'].map(value),[option('education_level'),option('education_status')]);if(form.querySelector('[name=no_degree]').checked)parts.push('No university degree');}
 else if(id==='preferences'){if(form.querySelector('[name=beta_preferences_present]')){section.querySelectorAll('fieldset').forEach(function(g){var selected=Array.from(g.querySelectorAll('input:checked')).map(function(c){return c.closest('label').textContent.trim();});if(selected.length)parts.push(g.querySelector('legend').textContent+': '+selected.join(', '));});section.querySelectorAll('.pay-expectation').forEach(function(g){var c=Array.from(g.querySelectorAll('input,select'));if(c[1].value||c[2].value)parts.push(c[0].selectedOptions[0].textContent+': '+c[2].value+' '+c[1].value+' '+c[3].selectedOptions[0].textContent.toLowerCase());});}else{parts=['availability','synchronous_preference','phone_preference'].map(option).concat(['schedule','employment_types','target_opportunity_types'].map(value));if(form.querySelector('[name=flexible]').checked)parts.push('Flexible hours preferred');}if(value('remote')==='1')parts.push('Remote work preferred');}
 else if(id==='optional'){section.querySelectorAll('input:not([type=checkbox]),textarea,select').forEach(function(c){if(meaningful(c.value))parts.push(c.closest('label').querySelector('span').textContent+': '+(c.tagName==='SELECT'?c.selectedOptions[0].textContent:c.value));});}
 section.querySelector('[data-section-summary]').textContent=parts.filter(meaningful).join(' · ')||'Not specified';}
function refresh(){form.querySelectorAll('.candidate-section').forEach(summarize);form.querySelector('[data-local-edit-status]').textContent=!form.hasAttribute('data-manual-draft')&&dirty()?'Edits on this page are not saved yet. Review changes to save the draft.':'';}
form.addEventListener('input',refresh);form.addEventListener('change',refresh);form.addEventListener('profile-editor-change',refresh);refresh();
form.querySelectorAll('[data-section-done]').forEach(function(button){button.addEventListener('click',function(){var section=button.closest('.candidate-section');refresh();section.open=false;section.querySelector('summary').focus();});});
function reveal(input){for(var p=input.parentElement;p&&p!==form;p=p.parentElement){if(p.tagName==='DETAILS')p.open=true;if(p.hasAttribute('data-language-row'))p.hidden=false;}languageCapacity();input.scrollIntoView({block:'center'});input.focus();}
var reporting=false;form.addEventListener('invalid',function(e){if(reporting)return;e.preventDefault();reveal(e.target);reporting=true;e.target.reportValidity();reporting=false;},true);
form.querySelectorAll('a[href^="#"]').forEach(function(link){link.addEventListener('click',function(e){var target=document.getElementById(link.hash.slice(1));if(target){e.preventDefault();reveal(target);}});});
var error=form.querySelector('[aria-invalid=true]');if(!error&&form.querySelector('#correction-error a'))error=document.getElementById(form.querySelector('#correction-error a').hash.slice(1));if(error)reveal(error);else if(form.dataset.focus){var target=document.getElementById(form.dataset.focus);if(target)reveal(target.querySelector('input:not([type=checkbox]),button')||target);}
form.addEventListener('submit',function(event){if(submitting){event.preventDefault();return;}Promise.resolve().then(function(){if(!event.defaultPrevented){submitting=true;form.querySelector('button[type=submit]').disabled=true;}});});
window.addEventListener('pageshow',function(){submitting=false;form.querySelector('button[type=submit]').disabled=false;});
})();"""
EDITOR_SHA256 = base64.b64encode(hashlib.sha256(EDITOR_SCRIPT.encode()).digest()).decode()


def actionable_issue(support, updates, canonical=None):
    """Feedback only; use the same validators as canonical persistence."""
    from wahojobs.profiles.canonical_v2 import _validate_string_list, MAX_DYNAMIC_LABEL_LENGTH
    from wahojobs.profiles.countries import normalize_country
    from wahojobs.profiles.item_experience import read_items
    if updates.get('domain_years_review'):
        try:
            if canonical is not None:
                domain_duration_editor.reviewed(updates['domain_years_review'],
                    canonical.get('experience', {}).get('years_by_domain') or {})
            else:
                duration_rows = json.loads(updates['domain_years_review'])['entries']
                for row in duration_rows:
                    value = row['years']
                    if type(value) is not str or not value or not 0 <= float(value) <= 80:
                        raise ValueError
        except (ValueError, TypeError, KeyError):
            return ('domain-duration-editor', 'Review the years for each field. Enter a whole number from 0 to 80, or use Remove duration if the value is unknown. Blank values are not saved as zero.')
    try:
        read_items(updates.get('item_experience', ''))
    except (ValueError, TypeError, KeyError):
        return ('section-skills', 'Review the optional experience details. Use a whole number of months from 0 to 960, keep each entry linked to one item, and leave unknown details blank.')
    try:
        normalize_country(updates.get('country', ''), allow_missing=True)
    except (ValueError, TypeError):
        return ('country', 'Enter a country name, such as Brazil, or leave it blank if you are unsure.')
    from wahojobs.profiles.review_entries import read_education_entries
    try:
        read_education_entries(updates.get('education_entries', ''))
    except (ValueError, TypeError):
        return ('education_entries', 'Review the education entries: remove empty or duplicate entries, keep each field within 128 characters, and use a four-digit year if provided.')
    for name, label in (('skills', 'Skills'), ('software_tools', 'Software and tools'),
            ('technical_skills', 'Technical skills'), ('writing_research_skills', 'Writing and research'),
            ('administrative_support_skills', 'Administration and support'), ('domain_specific_skills', 'Specialist skills'),
            ('specialties', 'Activities'), ('job_titles', 'Roles')):
        for index, item in enumerate(support.canonical_review.string_list(updates.get(name, ''))):
            errors = []
            _validate_string_list([item], errors)
            if errors:
                return (name, f'{label}, item {index + 1}: use at most {MAX_DYNAMIC_LABEL_LENGTH} characters per item. Edit it or separate distinct items with Add.')
    try:
        support.canonical_review.optional_years(updates.get('total_years'))
    except ValueError:
        return ('total_years', 'Enter a whole number of years from 0 to 80, or leave it blank.')
    return ('review-actions', 'Some details could not be saved as entered. Your edits are still here; review the changed sections and try again.')


def changed_profile_sections(before, after):
    """Compare candidate facts, excluding revision/provenance bookkeeping."""
    def facts(value):
        if isinstance(value, dict):
            return {k: facts(v) for k, v in value.items()
                    if k not in {'evidence', 'provenance', 'confidence', 'profile_id',
                                 'source_ordinals'}}
        if isinstance(value, list):
            return [facts(v) for v in value]
        return value
    return tuple(key for key in ('identity', 'location', 'languages', 'experience',
        'education', 'skills', 'preferences', 'credentials', 'constraints')
        if facts(before.get(key)) != facts(after.get(key)))


def change_summary(before, after, *, saved=False):
    sections = changed_profile_sections(before, after)
    if not sections:
        return ("<section id='profile-change-summary' role='status'><h2>No profile details have changed</h2>"
                "<p>This proposal is the same as your saved profile. No changes need to be applied.</p></section>")
    labels = dict(identity='Display name', location='Location', languages='Languages',
        experience='Experience and activities', education='Education and studies',
        skills='Skills and tools', preferences='Work preferences', credentials='Credentials',
        constraints='Constraints')
    detail = summary_sections({key: after[key] for key in sections})
    if 'identity' in sections:
        detail = '<p>Display name: ' + esc(after['identity'].get('display_name')) + '</p>' + detail
    return ("<section id='profile-change-summary' role='status'><h2>"
            + ('Profile changes saved' if saved else 'Changes awaiting confirmation') + '</h2><ul>'
            + ''.join('<li>' + esc(labels[key]) + '</li>' for key in sections) + '</ul>'
            + '<details><summary>' + ('See saved details' if saved else 'See proposed details')
            + '</summary>' + detail + '</details></section>')


def summary_sections(canonical):
    """Readable review of the same proposed values, with no taxonomy audit."""
    def section(label, items):
        values = [v for v in items if v not in (None, '', 'unknown', 'unspecified', 'not_specified')]
        return (f"<section class='profile-group'><h2>{esc(label)}</h2><ul>" + ''.join(f'<li>{esc(v)}</li>' for v in values) + '</ul></section>') if values else ''
    location = canonical.get('location', {})
    experience = canonical.get('experience', {})
    education = canonical.get('education', {})
    skills = canonical.get('skills', {})
    preferences = canonical.get('preferences', {})
    credentials = canonical.get('credentials', {})
    constraints = canonical.get('constraints', {})
    entries = education.get('entries', [])
    from wahojobs.profiles.item_experience import summary as item_summary
    from wahojobs.profiles.preference_presentation import preference_summary
    studies = [' · '.join(str(e[k]).replace('_', ' ') for k in ('qualification', 'field', 'institution', 'completion_year', 'status') if e.get(k) and e[k] != 'unknown') for e in entries]
    unpaired = unpaired_education(education)
    career = ([f"Total career experience: {experience['total_years']} years (not years in each profession)"] if experience.get('total_years') is not None else [])
    domain_years = experience.get('years_by_domain', ())
    if isinstance(domain_years, dict):
        domain_years = [dict(domain=domain, years=years) for domain, years in domain_years.items()]
    career.extend(f"Experience in {item['domain']}: {item['years']} years" for item in domain_years)
    study_details = [*studies, *unpaired.get('degrees', []), *unpaired.get('fields_or_domains', []), *unpaired.get('institutions', [])]
    if education.get('education_level') not in (None, '', 'unknown', 'not_specified'):
        study_details.append('Education level: ' + education['education_level'].replace('_', ' '))
    if education.get('completion_status') not in (None, '', 'unknown', 'not_specified'):
        study_details.append('Study status: ' + education['completion_status'].replace('_', ' '))
    return ''.join((
        section('Location', [', '.join(location[k] for k in ('city', 'region', 'country') if location.get(k))]),
        section('Languages', [f"{v['language']}" + (f" ({v['locale']})" if v.get('locale') else '') + f" — {str(v.get('proficiency') or 'Not specified').replace('_', ' ')}" for v in canonical.get('languages', [])]),
        section('Experience', list(dict.fromkeys([*experience.get('job_titles', []), *experience.get('recent_roles', []), *experience.get('specialties', []), *career]))),
        section('Optional experience details', [item_summary(i) for i in experience.get('item_details', [])]),
        section('Education and studies', study_details),
        section('Skills and tools', list(dict.fromkeys(v for values in skills.values() if isinstance(values, list) for v in values if isinstance(v, str)))),
        section('Work preferences', preference_summary(preferences)),
        section('Permissions and credentials', [location.get('work_authorization'), *location.get('eligible_countries', []), *credentials.get('licenses', []), *credentials.get('certifications', []), *credentials.get('jurisdictions', []), *credentials.get('security_clearances', [])]),
        section('Constraints and preferences', list(dict.fromkeys(v for values in constraints.values() if isinstance(values, list) for v in values if isinstance(v, str)))),
    ))

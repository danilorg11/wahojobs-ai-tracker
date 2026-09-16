"""Review existing scoped durations without creating new professional domains."""
from copy import deepcopy
from html import escape
import json
import re


FIELD = 'domain_years_review'


def form_value(existing):
    return json.dumps({'version': 1, 'entries': [
        {'domain': domain, 'years': str(years)} for domain, years in existing.items()
    ]})


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('invalid_domain_duration_review')
        result[key] = value
    return result


def reviewed(raw, existing):
    """Absent older controls preserve; an explicit snapshot can edit/remove.

    Domains are exact server-bound labels from the current review authority.
    Blank is invalid, never zero. Existing fractional values survive unchanged;
    new values use the conservative whole-year review convention.
    """
    if raw in (None, ''):
        return deepcopy(existing)
    if type(raw) is not str or len(raw) > 131072:
        raise ValueError('invalid_domain_duration_review')
    try:
        payload = json.loads(raw, object_pairs_hook=_pairs)
        if (type(payload) is not dict or set(payload) != {'version', 'entries'}
                or type(payload['version']) is not int or payload['version'] != 1
                or type(payload['entries']) is not list or len(payload['entries']) > len(existing)):
            raise ValueError
        result = {}
        for row in payload['entries']:
            if type(row) is not dict or set(row) != {'domain', 'years'}:
                raise ValueError
            domain, years = row['domain'], row['years']
            if type(domain) is not str or domain not in existing or domain in result or type(years) is not str:
                raise ValueError
            if years == str(existing[domain]):
                result[domain] = existing[domain]
            elif re.fullmatch(r'(?:0|[1-9][0-9]?)', years) and int(years) <= 80:
                result[domain] = int(years)
            else:
                raise ValueError
        return result
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ValueError('invalid_domain_duration_review') from None


def render(existing, raw):
    rows = [{'domain': domain, 'years': str(years)} for domain, years in existing.items()]
    try:
        payload = json.loads(raw)
        candidates = payload['entries']
        if (payload.get('version') == 1 and type(candidates) is list
                and all(type(row) is dict and set(row) == {'domain', 'years'}
                    and type(row['domain']) is str and row['domain'] in existing
                    and type(row['years']) is str for row in candidates)
                and len({row['domain'] for row in candidates}) == len(candidates)):
            rows = candidates
    except (ValueError, TypeError, KeyError, RecursionError, AttributeError):
        pass
    if not existing:
        return ''
    return ("<div id='domain-duration-editor' tabindex='-1' data-domain-duration-editor>"
        "<h3>Experience by field</h3><p>These years describe each field, separately from total career length. "
        "Edit a whole number from 0 to 80, or Remove a duration if it is unknown or incorrect. "
        "Existing fractional durations are kept unless you change them.</p>"
        + ''.join("<div class='correction-item' data-domain-duration-row data-domain='" + escape(row['domain'], quote=True)
            + "'><label class='review-field'><span>Years in " + escape(row['domain'])
            + "</span><input data-domain-duration-value inputmode='decimal' value='" + escape(row['years'], quote=True)
            + "'></label><button type='button' class='button-quiet' data-remove-domain-duration>Remove duration</button></div>" for row in rows)
        + "</div>")


SCRIPT = """(function(){'use strict';var editor=document.querySelector('[data-domain-duration-editor]');if(!editor)return;var form=editor.closest('form'),hidden=form.querySelector('[name=domain_years_review]');
function sync(){hidden.value=JSON.stringify({version:1,entries:Array.from(editor.querySelectorAll('[data-domain-duration-row]')).filter(function(row){return !row.hidden;}).map(function(row){return {domain:row.dataset.domain,years:row.querySelector('[data-domain-duration-value]').value};})});hidden.dispatchEvent(new Event('profile-editor-change',{bubbles:true}));}
editor.addEventListener('input',sync);editor.addEventListener('click',function(e){var button=e.target.closest('[data-remove-domain-duration]');if(!button)return;var row=button.closest('[data-domain-duration-row]'),input=row.querySelector('input');row.hidden=true;input.disabled=true;sync();var undo=document.createElement('button');undo.type='button';undo.className='button-quiet';undo.dataset.undoDomainDuration='';undo.textContent='Undo removal: '+row.dataset.domain;row.after(undo);undo.addEventListener('click',function(){row.hidden=false;input.disabled=false;sync();undo.remove();input.focus();});undo.focus();});})();"""

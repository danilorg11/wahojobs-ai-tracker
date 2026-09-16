"""Optional navigation into the existing confirmed profile correction flow.

Navigation is not authority to change a profile, view another account's run or
claim recommendation membership. Destination routes still authorize normally.
"""
from html import escape
import re
from urllib.parse import parse_qs, urlencode, urlsplit


TOOL_FOCUS = {'normalized': 'skills', 'software_tools': 'software_tools',
              'technical': 'technical_skills', 'domain_specific': 'domain_specific_skills'}
FOCUS_FIELDS = frozenset({'education', 'languages', 'experience', 'preferences', 'location', *TOOL_FOCUS.values()})


def _tool_correction(row):
    """Route recorded tool gaps to an existing editable collection, without answers."""
    from wahojobs.candidate_condition_comparisons import _tool_groups
    quote = re.sub(r'\*\*([^*]+)\*\*', r'\1', row['source']['quote'])
    clean = re.sub(r'\s+(?:required|preferred)\.?$', '', quote, flags=re.I)
    match = re.fullmatch(r'(Working proficiency|Proficiency|Experience|Comfortable) (?:in|with) (.+)', clean, re.I)
    parsed = _tool_groups(match[2]) if match else None
    if not parsed or parsed[1] == 'unresolved_slash':
        return None
    groups, _ = parsed
    facts = row.get('profile_facts', [])
    reported = [f['value'] for f in facts if isinstance(f.get('value'), dict)
                and f.get('field_path', '').startswith('experience.item_details[')]
    # Existing self-reports cannot settle domain-specific use or duration.
    if reported and re.search(r'\bfor\b| [—–] ', match[2], re.I):
        return None
    usable = {d['label'].casefold() for d in reported if d.get('contexts') and
              (match[1].lower() == 'experience' or
               (match[1].lower() == 'working proficiency' and d.get('autonomy') in ('independent', 'complex')))}
    pending = [g for g in groups if not any(t.casefold() in usable for t in g)]
    if not pending:
        return None
    names = {t.casefold() for g in pending for t in g}
    mentions = [f for f in facts if isinstance(f.get('value'), str) and f['value'].casefold() in names]
    for fact in mentions:
        path = re.match(r'skills\.([a-z_]+)\[', fact.get('field_path', ''))
        if path and path[1] in TOOL_FOCUS:
            return TOOL_FOCUS[path[1]], 'Your listed tools are already saved. Review optional experience details for those tools.'
    if mentions:
        return None  # No direct item editor for this legacy collection.
    return 'software_tools', 'Your saved tool details stay in place. Add a missing tool and its experience details only if they describe you.'


def safe_opportunity_return(value):
    if not isinstance(value, str) or len(value) > 256 or re.search(r'[\s\\%\x00-\x1f\x7f]', value):
        return None
    from wahojobs.authenticated_variant_details import parse_variant_query
    from wahojobs.public_job_page import parse_public_job_path
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.fragment or not value.startswith('/job/'):
        return None
    if parse_public_job_path(parsed.path) is None:
        return None
    query = parse_variant_query(parsed.query)
    if not query or 'variant' not in query or not set(query) <= {'variant', 'run'}:
        return None
    return parsed.path + '?' + urlencode(query)


def navigation_from_fields(form, *, allow_focus=True):
    """Strip only bounded navigation fields; leave existing form validation intact."""
    form = dict(form)
    keys = {'return_to', 'focus'} & set(form)
    if not keys:
        return form, None
    if 'return_to' not in keys or (not allow_focus and 'focus' in keys):
        raise ValueError('invalid_profile_return')
    values = {key: form.pop(key) for key in keys}
    if any(type(v) is not list or len(v) != 1 for v in values.values()):
        raise ValueError('invalid_profile_return')
    target = safe_opportunity_return(values['return_to'][0])
    focus = values.get('focus', [''])[0]
    if target is None or (focus and focus not in FOCUS_FIELDS):
        raise ValueError('invalid_profile_return')
    return form, {'return_to': target, 'focus': focus}


def correction_entry_navigation(target):
    """Only the start page accepts navigation query parameters."""
    if not isinstance(target, str) or len(target) > 768:
        raise ValueError('invalid_profile_return')
    parsed = urlsplit(target)
    if parsed.path != '/account/profile' or not re.search(r'(?:^|&)(?:return_to|focus)=', parsed.query):
        return target, None
    values = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
    rest, nav = navigation_from_fields(values)
    if rest != {'correction': ['start']} or parsed.fragment or parsed.scheme or parsed.netloc:
        raise ValueError('invalid_profile_return')
    return '/account/profile?correction=start', nav


def navigation_fields(nav, *, include_focus=True):
    if not nav:
        return ''
    keys = ('return_to', 'focus') if include_focus else ('return_to',)
    return ''.join(f"<input type='hidden' name='{key}' value='{escape(nav[key], quote=True)}'>" for key in keys)


def cancel_link(nav):
    return (f"<p><a href='{escape(nav['return_to'], quote=True)}'>Cancel and return to opportunity</a></p>"
            if nav else '')


def render_profile_update(packet, return_to, *, general_fallback=False, for_recommendations=False):
    general_label = ('Edit Wahojobs profile &amp; preferences for recommendations' if for_recommendations
                     else 'Review profile &amp; preferences')
    general = ("<p class='candidate-profile-next'><a href='/account/profile'>" + general_label + '</a> '
               '<span>(optional)</span></p>') if general_fallback else ''
    target = safe_opportunity_return(return_to)
    if not packet or not target:
        return general
    # Navigation only. Existing confirmed facts are retained in the editor;
    # a correction does not establish employer acceptance or complete eligibility.
    tool_action = next((action for r in packet.get('comparisons', [])
                       if r['kind'] == 'tools' and r['status'] == 'not_established'
                       and (action := _tool_correction(r))), None)
    focus, guidance = tool_action or (None, '')
    if not focus and any(r['kind'] == 'education' and r['status'] == 'not_established'
                         and not r['supported_parts'] for r in packet.get('comparisons', [])):
        focus = 'education'
    from wahojobs.candidate_decision import missing_language_fact
    if not focus and any(missing_language_fact(c) for c in packet.get('language_comparisons', [])):
        focus, guidance = 'languages', 'Review your own language level if it is missing or inaccurate.'
    if not focus and any(r['kind'] == 'professional_background' and r['status'] == 'not_established'
                         and not r.get('supported_parts') for r in packet.get('comparisons', [])):
        focus, guidance = 'experience', 'You can add missing roles or activities. This does not establish the required depth or years in a specific field.'
    if not focus and any(r['kind'] == 'workload' and r['status'] == 'contradicted'
                         for r in packet.get('comparisons', [])):
        focus, guidance = 'preferences', 'Review your work preferences if they have changed. A preference does not confirm available hours.'
    if not focus:
        return general
    link = '/account/profile?' + urlencode({'correction': 'start', 'return_to': target, 'focus': focus})
    label = {'education': 'Review education details', 'languages': 'Review language details',
             'experience': 'Review experience details', 'preferences': 'Review work preferences'}.get(focus,
             'Review skills and experience')
    if for_recommendations:
        label = 'Edit Wahojobs ' + label.removeprefix('Review ') + ' for recommendations'
    return (f"<div class='candidate-profile-next'><p><a class='candidate-profile-update' href='{escape(link, quote=True)}'>{label}</a> <span>(optional)</span></p>"
        + ('<p>' + escape(guidance) + '</p>' if guidance else '') + '</div>')

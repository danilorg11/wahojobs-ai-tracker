"""Optional navigation into the existing confirmed profile correction flow.

Navigation is not authority to change a profile, view another account's run or
claim recommendation membership. Destination routes still authorize normally.
"""
from html import escape
import re
from urllib.parse import parse_qs, urlencode, urlsplit


FOCUS_FIELDS = frozenset({'education', 'software_tools'})


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


def render_profile_update(packet, return_to):
    target = safe_opportunity_return(return_to)
    if not packet or not target:
        return ''
    # Source ambiguity, related-degree acceptance and proficiency cannot be
    # settled by adding a skill label. Only missing personal facts get a link.
    eligible = [r for r in packet.get('comparisons', [])
                if r['status'] == 'not_established' and not r['supported_parts']]
    focus = ('software_tools' if any(r['kind'] == 'tools' for r in eligible) else
             'education' if any(r['kind'] == 'education' for r in eligible) else None)
    if not focus:
        return ''
    link = '/account/profile?' + urlencode({'correction': 'start', 'return_to': target, 'focus': focus})
    return f"<p class='candidate-note'><a class='candidate-profile-update' href='{escape(link, quote=True)}'>Update profile</a> <span>(optional)</span></p>"

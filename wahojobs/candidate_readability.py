"""Display-only labels; stored facts and timestamps remain unchanged."""
from datetime import datetime
import json
import re


EDUCATION_KIND_LABELS = {
    'bachelor': 'Bachelor’s degree', 'master': 'Master’s degree',
    'doctorate': 'Doctorate', 'phd': 'PhD', 'associate': 'Associate degree',
    'high_school': 'High school', 'secondary': 'Secondary education',
    'professional_degree': 'Professional degree', 'professional': 'Professional qualification',
    'advanced_degree': 'Advanced degree', 'technical': 'Technical qualification',
}
_DEGREE_WORDS = {
    'bachelor': r"bachelor|b[.]?sc[.]?|b[.]?a[.]?",
    'master': r"master|m[.]?sc[.]?|m[.]?a[.]?",
    'doctorate': r"doctor|ph[.]?d[.]?", 'phd': r"ph[.]?d[.]?|doctor of philosophy",
    'associate': r"associate", 'high_school': r"high school",
}


def education_entry_title(entry):
    """Describe the explicitly selected type without guessing from a study field."""
    kind = entry.get('kind')
    label = EDUCATION_KIND_LABELS.get(kind, '')
    qualification = entry.get('qualification') or ''
    field = entry.get('field') or ''
    pattern = _DEGREE_WORDS.get(kind)
    already_named = bool(pattern and re.match('(?:' + pattern + r')(?:\s|\b)', qualification, re.I))
    if label and qualification and not already_named:
        qualification = (label + ' in ' + qualification if qualification.casefold() == field.casefold()
                         else label + ' · ' + qualification)
    else:
        qualification = qualification or label
    if field and field.casefold() not in qualification.casefold():
        qualification += (' in ' if qualification else '') + field
    return qualification


# The rendered editor uses the same explicit type labels as server summaries.
EDUCATION_TITLE_SCRIPT = (
    'var educationLabels=' + json.dumps(EDUCATION_KIND_LABELS) + ';'
    'var educationDegreeWords=' + json.dumps(_DEGREE_WORDS) + ';'
    + r"""
function educationEntryTitle(item){
 var label=educationLabels[item.kind]||'',qualification=item.qualification||'',field=item.field||'';
 var pattern=educationDegreeWords[item.kind],named=pattern&&new RegExp('^(?:'+pattern+')(?:\\s|\\b)','i').test(qualification);
 if(label&&qualification&&!named){qualification=label+(qualification.toLowerCase()===field.toLowerCase()?' in ':' · ')+qualification;}
 else{qualification=qualification||label;}
 if(field&&qualification.toLowerCase().indexOf(field.toLowerCase())===-1)qualification+=(qualification?' in ':'')+field;
 return qualification;
}
""")


def readable_date(value):
    if not value:
        return ''
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return str(value)
    label = f'{date.day} {date:%b %Y}'
    if 'T' in str(value) or ' ' in str(value):
        zone = date.strftime('%z')
        label += f' at {date:%H:%M}' + (' UTC' if zone == '+0000' else f' {zone}' if zone else '')
    return label


def education_summary(education):
    from wahojobs.profiles.review_entries import unpaired_education
    labels = EDUCATION_KIND_LABELS
    rows = []
    for entry in education.get('entries', []):
        qualification = education_entry_title(entry)
        parts = [qualification, entry.get('institution'), entry.get('completion_year')]
        if entry.get('status') not in (None, '', 'unknown'):
            parts.append(entry['status'].replace('_', ' ').capitalize())
        rows.append(' · '.join(str(v) for v in parts if v))
    unpaired = unpaired_education(education)
    rows.extend(v for key in ('degrees', 'fields_or_domains', 'institutions') for v in unpaired.get(key, []))
    level = education.get('education_level')
    if level not in (None, '', 'unknown', 'not_specified') and not any(
            entry.get('kind') == level for entry in education.get('entries', [])):
        rows.append(labels.get(level, level.replace('_', ' ').capitalize()))
    completion = education.get('completion_status')
    if completion not in (None, '', 'unknown', 'not_specified'):
        rows.append('Study status: ' + completion.replace('_', ' ').capitalize())
    return list(dict.fromkeys(rows))


def credential_status_label(value):
    return {'absent': 'No specialized credentials reported',
            'explicit': 'Professional credentials reported',
            'in_progress': 'Working toward a professional credential'}.get(value, '')

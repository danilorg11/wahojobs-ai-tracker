"""Optional, explicitly reviewed self-reports linked to existing profile items.

Labels remain in the established skill/activity collections. Only an item with
optional details gets a durable ID; that ID survives a reviewed rename. These
facts are not independent verification and are never scoring inputs.
"""
from copy import deepcopy
import json
import re
import unicodedata

FIELDS = {
    'skills': ('skills', 'normalized'),
    'software_tools': ('skills', 'software_tools'),
    'technical_skills': ('skills', 'technical'),
    'writing_research_skills': ('skills', 'writing_research'),
    'administrative_support_skills': ('skills', 'administrative_support'),
    'domain_specific_skills': ('skills', 'domain_specific'),
    'specialties': ('experience', 'specialties'),
}
CONTEXTS = {'study': 'Study or training', 'projects': 'Personal or volunteer projects',
            'professional': 'Paid or professional work'}
AUTONOMY = {'unknown': 'Not specified',
            'guided': 'I use it with guidance or step-by-step instructions',
            'independent': 'I complete routine tasks independently',
            'complex': 'I handle unfamiliar or complex tasks independently'}
KEYS = frozenset({'item_id', 'field', 'label', 'contexts', 'autonomy', 'months', 'basis'})
PREFIX = 'experience.item_details'
MAX_ITEMS = 16


def label_key(value):
    return ' '.join(unicodedata.normalize('NFC', value).split()).casefold()


def linked(profile, item):
    root, field = FIELDS[item['field']]
    return item['label'] in profile.get(root, {}).get(field, [])


def canonical_items(value):
    if type(value) is not list or len(value) > MAX_ITEMS:
        raise ValueError('invalid_item_experience')
    result, ids, links = [], set(), set()
    for entry in value:
        if type(entry) is not dict or set(entry) != KEYS:
            raise ValueError('invalid_item_experience')
        item = deepcopy(entry)
        if (type(item['item_id']) is not str or not re.fullmatch(r'[a-f0-9]{32}', item['item_id'])
                or item['field'] not in FIELDS or item['basis'] != 'self_reported'
                or type(item['label']) is not str or not 1 <= len(item['label']) <= 128
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in item['label'])
                or type(item['contexts']) is not list
                or any(type(c) is not str or c not in CONTEXTS for c in item['contexts'])
                or len(item['contexts']) != len(set(item['contexts']))
                or item['autonomy'] not in AUTONOMY
                or (item['months'] is not None and
                    (type(item['months']) is not int or not 0 <= item['months'] <= 960))):
            raise ValueError('invalid_item_experience')
        item['label'] = ' '.join(unicodedata.normalize('NFC', item['label']).split())
        item['contexts'] = sorted(item['contexts'])
        link = (item['field'], label_key(item['label']))
        if item['item_id'] in ids or link in links or not item['label']:
            raise ValueError('duplicate_item_experience')
        ids.add(item['item_id']); links.add(link)
        result.append(item)
    return sorted(result, key=lambda item: item['item_id'])


def read_items(raw):
    if raw == '':
        return None  # Older/narrower editors cannot erase undisplayed details.
    if type(raw) is not str or len(raw) > 32768:
        raise ValueError('invalid_item_experience')
    try:
        return canonical_items(json.loads(raw))
    except (TypeError, KeyError, json.JSONDecodeError):
        raise ValueError('invalid_item_experience') from None


def prune_unlinked(profile):
    """Deleting a label removes only its own optional data on confirmation."""
    if 'item_details' in profile['experience']:
        profile['experience']['item_details'] = [i for i in profile['experience']['item_details'] if linked(profile, i)]


def with_reviewed_items(profile, raw, base):
    from wahojobs.profiles.canonical_v2 import (
        validate_canonical_profile_v2, _material_field_paths, FIELD_PATH_VERSION,
    )
    items = read_items(raw)
    if items is None:
        return profile
    previous = {i['item_id']: i for i in base['experience'].get('item_details', [])}
    for item in items:
        if not linked(profile, item):
            raise ValueError('unlinked_item_experience')
        old = previous.get(item['item_id'])
        if old and (old['field'] != item['field'] or
                    (old['label'] != item['label'] and linked(profile, old))):
            raise ValueError('item_experience_identity_changed')
    result = deepcopy(profile)
    if items:
        result['experience']['item_details'] = items
    else:
        result['experience'].pop('item_details', None)
    # Preserve unchanged record provenance even if deletion reorders indices.
    old_refs = {r['field_path']: r for r in base['provenance']['field_sources']}
    refs = [r for r in result['provenance']['field_sources'] if not r['field_path'].startswith(PREFIX)]
    for index, item in enumerate(items):
        old_index = next((n for n, old in enumerate(base['experience'].get('item_details', [])) if old == item), None)
        prefix = f'{PREFIX}[{index}]'
        for path in _material_field_paths(result):
            if not path.startswith(prefix + '.'):
                continue
            old_path = path.replace(prefix, f'{PREFIX}[{old_index}]', 1)
            old = old_refs.get(old_path) if old_index is not None else None
            refs.append(dict(deepcopy(old), field_path=path) if old else dict(
                field_path=path, path_version=FIELD_PATH_VERSION, source_ordinals=[2],
                source_kind='user_correction', explicit=True))
    result['provenance']['field_sources'] = sorted(refs, key=lambda p: (p['field_path'].casefold(), p['field_path']))
    return validate_canonical_profile_v2(result)


def summary(item):
    parts = [CONTEXTS[c] for c in item['contexts']]
    if item['autonomy'] != 'unknown': parts.append(AUTONOMY[item['autonomy']])
    if item['months'] is not None: parts.append('less than one month using this item' if item['months'] == 0 else f"about {item['months']} months using this item")
    return item['label'] + ' — ' + ('; '.join(parts) or 'experience details not specified') + ' (self-reported)'

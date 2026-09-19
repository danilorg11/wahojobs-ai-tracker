"""Candidate controls and summaries for the existing preference contracts."""
from copy import deepcopy
from html import escape

from wahojobs.profiles.preference_model import (
    MAX_COMPENSATION_EXPECTATIONS, canonicalize_profile_preferences_v2,
    preference_model_for_v2_editor, preference_model_to_legacy_preferences,
    profile_preference_control_catalog_v2,
)


def preference_summary(preferences):
    model = preferences.get('preference_model')
    if model is not None:
        model = preference_model_for_v2_editor(model)
        rows = []
        for dimension in profile_preference_control_catalog_v2()['dimensions']:
            values = model
            for part in dimension['path']:
                values = values[part]
            if values:
                labels = {c['code']: c['label'] for c in dimension['choices']}
                rows.append(dimension['title'] + ': ' + ', '.join(labels[v] for v in values))
        rows.extend(f"{e['minimum_kind'].title()} minimum: {e['currency']} {e['amount']}/{e['period']}"
                    for e in model['compensation_expectations'])
        # These existing legacy values are independent of the typed dimensions.
        if preferences.get('availability') not in (None, '', 'unknown', 'unspecified',
                *[value.replace('_', '-') for value in model['workloads']]):
            rows.append('Workload or start preference: ' + preferences['availability'].replace('_', ' '))
        # Stored manual free text is independent of the typed catalog. A typed
        # workload edit must neither erase it nor make it disappear from review.
        for key, label in (('target_opportunity_types', 'Work interests'),
                           ('preferred_task_types', 'Task preferences')):
            extra = [value for value in preferences.get(key, []) if value not in model['job_interests']]
            if key == 'preferred_task_types' and set(preferences.get(key, [])) == set(preferences.get('target_opportunity_types', [])):
                continue
            if extra:
                rows.append(label + ': ' + ', '.join(extra))
        return rows or ['No work preferences or pay minimum specified.']
    rows = []
    labels = {'availability': 'Workload or start preference', 'synchronous_preference': 'Team coordination',
              'phone_preference': 'Phone work'}
    for key, label in labels.items():
        value = preferences.get(key)
        if value not in (None, '', 'unknown', 'unspecified'):
            rows.append(label + ': ' + str(value).replace('_', ' '))
    if preferences.get('flexible'):
        rows.append('Flexible hours preferred')
    for key, label in (('schedule', 'Schedule'), ('employment_types', 'Workload and contract preferences'),
                       ('target_opportunity_types', 'Work interests')):
        if preferences.get(key):
            rows.append(label + ': ' + ', '.join(str(v).replace('_', ' ') for v in preferences[key]))
    return rows or ['No work preferences specified.']


def _field(dimension):
    return 'beta_preference_' + '_'.join(dimension['path'])


def render_preference_editor(model, submitted=None):
    """Ordinary HTML controls; no hidden JSON or browser-authored shadows."""
    model = preference_model_for_v2_editor(model)
    pieces = ["<input type='hidden' name='beta_preferences_present' value='1'>",
              "<p>Select every option you would consider. Leave a group empty for no preference. "
              "Workload choices are preferences; they do not ban other schedules. "
              "Missing employer information stays unknown.</p>"]
    for dimension in profile_preference_control_catalog_v2()['dimensions']:
        values = model
        for part in dimension['path']:
            values = values[part]
        # Keep the accepted onboarding scope: expose only already confirmed job interests.
        if dimension['id'] == 'job_interests' and not values:
            continue
        name = _field(dimension)
        selected = submitted.get(name, []) if submitted is not None else values
        pieces.append("<fieldset class='choice-fieldset'><legend>" + escape(dimension['title']) + "</legend><div class='choice-grid'>")
        for choice in dimension['choices']:
            pieces.append(f"<label class='choice-card'><input type='checkbox' name='{name}' value='{choice['code']}'"
                          + (' checked' if choice['code'] in selected else '') + '><span>' + escape(choice['label']) + '</span></label>')
        pieces.append('</div></fieldset>')
    pieces.append("<details class='pay-expectations'" + (' open' if model['compensation_expectations'] else '') + "><summary>Pay minimums (optional)</summary>"
                  "<p>Each minimum applies only to the same currency and pay period. Amounts are not converted. "
                  "A strict minimum can exclude opportunities with missing pay information. Advertised pay is not guaranteed earnings.</p>")
    for index in range(MAX_COMPENSATION_EXPECTATIONS):
        item = model['compensation_expectations'][index] if index < len(model['compensation_expectations']) else {}
        def value(key):
            return (submitted.get(f'beta_pay_{index}_{key}', [''])[0] if submitted is not None else item.get(key, ''))
        populated = any(value(k) for k in ('amount', 'currency'))
        pieces.append("<details class='pay-expectation'" + (' open' if populated else '') + f"><summary>Pay minimum {index + 1}" + (' — set' if populated else ' — optional') + "</summary><div class='review-grid'>")
        for key, label in (('minimum_kind', 'Minimum type'), ('amount', 'Amount'), ('currency', 'Currency'), ('period', 'Pay period')):
            name = f'beta_pay_{index}_{key}'
            pieces.append(f"<label class='review-field'><span>{label}</span>")
            options = {'minimum_kind': {'preferred':'Preferred minimum', 'strict':'Strict minimum'},
                       'period': {'hour':'Per hour', 'month':'Per month', 'year':'Per year'}}.get(key)
            if options:
                pieces.append(f"<select name='{name}'>" + ''.join(f"<option value='{k}'" + (' selected' if value(key) == k else '') + f'>{label}</option>' for k,label in options.items()) + '</select>')
            else:
                pieces.append(f"<input name='{name}' value='{escape(value(key), quote=True)}'" + (" inputmode='decimal' placeholder='e.g. 15'" if key == 'amount' else " maxlength='3' placeholder='e.g. USD'") + '>')
            pieces.append('</label>')
        pieces.append('</div><p>Leave amount and currency blank to remove this minimum.</p></details>')
    pieces.append('</details>')
    return ''.join(pieces)


def read_preference_editor(form, current):
    """Accept exactly the rendered, bounded controls and reject duplicate values."""
    model = preference_model_for_v2_editor(current)
    expected = {'beta_preferences_present'}
    if form.get('beta_preferences_present') != ['1']:
        raise ValueError('invalid_preference_review')
    for dimension in profile_preference_control_catalog_v2()['dimensions']:
        parent = model
        for part in dimension['path'][:-1]:
            parent = parent[part]
        key = dimension['path'][-1]
        if dimension['id'] == 'job_interests' and not parent[key]:
            continue
        name = _field(dimension)
        expected.add(name)
        values = form.get(name, [])
        allowed = {c['code'] for c in dimension['choices']}
        if type(values) is not list or any(type(v) is not str or v not in allowed for v in values) or len(values) != len(set(values)):
            raise ValueError('invalid_preference_review')
        parent[key] = values
    expectations = []
    for index in range(MAX_COMPENSATION_EXPECTATIONS):
        item = {}
        for key in ('minimum_kind', 'amount', 'currency', 'period'):
            name = f'beta_pay_{index}_{key}'
            expected.add(name)
            value = form.get(name)
            if type(value) is not list or len(value) != 1 or type(value[0]) is not str:
                raise ValueError('invalid_preference_review')
            item[key] = value[0].strip()
        if item['amount'] or item['currency']:
            expectations.append(item)
    if set(form) != expected.intersection(form):
        raise ValueError('invalid_preference_review')
    model['compensation_expectations'] = expectations
    return canonicalize_profile_preferences_v2(model)


def with_reviewed_preferences(profile, model):
    """Replace only reviewed preference authority and server-derived mirrors."""
    from wahojobs.profiles.canonical_v2 import validate_canonical_profile_v2, _material_field_paths, FIELD_PATH_VERSION
    from wahojobs.profiles.preference_model import validate_profile_preferences, update_preference_legacy_mirror
    result = validate_canonical_profile_v2(profile)
    model = validate_profile_preferences(model)
    existing = result['preferences'].get('preference_model')
    if existing is None:
        raise ValueError('preference_model_not_available')
    if preference_model_for_v2_editor(existing) == preference_model_for_v2_editor(model):
        return result  # An unchanged V1 model remains V1 with its provenance intact.
    before_preferences = deepcopy(result['preferences'])
    result['preferences'] = update_preference_legacy_mirror(result['preferences'], existing, model)
    result['preferences']['preference_model'] = deepcopy(model)
    changed = {key for key in result['preferences'] if before_preferences.get(key) != result['preferences'][key]}
    def affected(path):
        return any(path == 'preferences.' + key or path.startswith(('preferences.' + key + '.', 'preferences.' + key + '[')) for key in changed)
    refs = [r for r in result['provenance']['field_sources'] if not affected(r['field_path'])]
    for path in _material_field_paths(result):
        if affected(path):
            refs.append(dict(field_path=path, path_version=FIELD_PATH_VERSION,
                             source_ordinals=[2], source_kind='user_correction', explicit=True))
    result['provenance']['field_sources'] = sorted(refs, key=lambda r: (r['field_path'].casefold(), r['field_path']))
    return validate_canonical_profile_v2(result)


def candidate_workload_context(profile, packet, match=None):
    """Use confirmed wishes/firm constraints and actual evaluated workload only."""
    from wahojobs.profiles.preference_model import confirmed_hard_workload, effective_preference_authority
    from wahojobs.profiles.canonical_v2 import CanonicalProfileV2Error
    from wahojobs.profiles.preference_model import ProfilePreferenceModelError
    try:
        model = (effective_preference_authority(profile)[0] or {}) if isinstance(profile, dict) else {}
    except (CanonicalProfileV2Error, ProfilePreferenceModelError):
        # Source-only renderers can have a partial profile view. It cannot author
        # preference claims. The matching consumer still requires valid V2.
        return dict(guidance=None, state='preference', outcome=None)
    wanted = model.get('workloads') or []
    hard = confirmed_hard_workload(profile) if isinstance(profile, dict) else ()
    choices = hard or tuple(wanted)
    if len(choices) != 1:
        return dict(guidance=None, state='preference', outcome=None)
    label = choices[0].replace('_', '-')
    outcomes = (match or {}).get('_workload_preference_outcomes', [])
    outcome = next((o.get('outcome') for o in outcomes if o.get('criterion_id') == 'preferences.workloads'), None)
    if hard:
        if outcome == 'fail':
            state, guidance = 'hard_conflict', f'Your {label}-only requirement conflicts with this posting’s workload.'
        elif outcome == 'pass':
            state, guidance = 'supported', f'This posting lists {label} work; confirm the actual hours fit your availability.'
        else:
            state, guidance = 'hard_unresolved', f'You only consider {label} work. Confirm the employer offers that schedule before proceeding.'
    elif outcome == 'fail':
        other = 'full-time' if choices[0] == 'part_time' else 'part-time'
        state, guidance = 'soft_difference', f'You prefer {label} work; this posting lists {other} work. Confirm whether your preferred schedule is possible.'
    else:
        state, guidance = 'preference', f'You prefer {label} work. Explain your availability and confirm the schedule with the employer.'
    return dict(guidance=guidance, state=state, outcome=outcome)


def candidate_workload_guidance(profile, packet, match=None):
    return candidate_workload_context(profile, packet, match)['guidance']

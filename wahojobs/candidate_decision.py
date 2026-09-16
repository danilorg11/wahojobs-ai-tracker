"""Candidate presentation of existing evidence and decisions; never admission.

No new requirement interpretation, matching, persistence or authority lives here.
Source quotes and consulted profile facts retain their original provenance.
"""
from copy import deepcopy
from html import escape
import re


def same_source(reference, packet):
    return bool(packet.get('source_hash')) and all(reference.get(a) == packet.get(b) for a, b in (
        ('job_id', 'job_id'), ('external_id', 'external_id'),
        ('source_url', 'url'), ('material_content_sha256', 'source_hash')))


def _transferable_links(accepted):
    """Display recorded relationships only; never infer a task from prose."""
    facts, profile_facts = accepted.get('facts'), accepted.get('profile_facts')
    links = accepted.get('task_links')
    if not all(isinstance(values, (list, tuple)) and values
            for values in (facts, profile_facts, links, accepted.get('scope_evidence'))):
        return []
    displayed = []
    seen = set()
    for link in links:
        if not isinstance(link, dict):
            return []
        fact = link.get('profile_fact') or {}
        if (link.get('support_kind') != 'transferable_activity'
                or not isinstance(link.get('task_family'), str) or not link['task_family']
                or not isinstance(fact, dict)
                or not all(isinstance(fact.get(k), str) and fact[k] for k in ('path', 'text'))
                or not all(isinstance(link.get(k), str) and link[k] for k in ('quote', 'block_reference'))
                or not any(isinstance(source, dict) and all(source.get(k) == link[k]
                    for k in ('quote', 'block_reference')) for source in facts)
                or not any(isinstance(profile, dict) and all(profile.get(k) == fact[k]
                    for k in ('path', 'text')) for profile in profile_facts)):
            return []
        identity = (link['quote'], fact['path'], fact['text'])
        if identity not in seen:
            displayed.append(deepcopy(link))
            seen.add(identity)
    return displayed[:2]


def attach_decision(packet, match, *, profile=None):
    """Project only explanations actually recorded by the current computation."""
    reasons = []
    has_reported_support = False
    accepted = match.get('accepted_task_fit') or {}
    packet['transferable_task_links'] = []
    if (same_source(accepted.get('source_reference') or {}, packet)
            and accepted.get('facts') and accepted.get('profile_facts')):
        if accepted.get('basis') == 'transferable_activity':
            packet['transferable_task_links'] = _transferable_links(accepted)
            reasons.extend('Your confirmed activity “' + link['profile_fact']['text']
                + '” can transfer to a related task in this opportunity.'
                for link in packet['transferable_task_links'])
            has_reported_support = bool(packet['transferable_task_links'])
        elif accepted.get('basis') in (None, 'confirmed_ai_work'):
            reasons.append('Your confirmed profile describes AI evaluation or annotation work that overlaps with the employer’s tasks.')
            has_reported_support = True
    # Use recorded evidence tuples, not narrative templates which can name
    # particular skills that were never consulted for this candidate.
    for item in (match.get('affirmative_fit') or {}).get('supported_evidence') or []:
        requirement, fact = item.get('requirement'), item.get('profile_evidence')
        if (item.get('source') in ('accepted_source_task', 'transferable_activity')
                or requirement == 'AI evaluation or annotation tasks'):
            continue  # task explanations require the exact accepted-task packet
        if isinstance(requirement, str) and isinstance(fact, str) and requirement and fact:
            reasons.append((f'Your stated interest relates to {requirement}; interest does not establish experience.'
                if item.get('source') == 'preference' else
                f'Your profile lists {fact}, which relates to the role’s {requirement} focus.'))
            has_reported_support = has_reported_support or item.get('source') != 'preference'
    packet['decision_reasons'] = list(dict.fromkeys(reasons))[:2]
    packet['decision_has_reported_support'] = has_reported_support
    packet['decision_short_reason'] = _short_reason(packet, match)
    # A rendering context, never new candidate evidence or a persisted update.
    packet['decision_profile_context'] = {
        'country': (profile or {}).get('location', {}).get('country') or '',
    }
    if profile is not None:
        from wahojobs.profiles.preference_presentation import candidate_workload_context
        packet['decision_profile_context']['workload'] = candidate_workload_context(profile, packet, match)
    packet['language_comparisons'] = [deepcopy(c) for c in match.get('source_language_checks') or []
        if same_source(c.get('source_reference') or {}, packet)]
    reviewed = match.get('source_task_fit') or {}
    packet['decision_consumed_conditions'] = []
    if same_source(reviewed.get('source_reference') or {}, packet):
        for condition in reviewed.get('conditions') or []:
            source = condition.get('source') or {}
            if any(source == row.get('source') for row in packet.get('comparisons') or []):
                packet['decision_consumed_conditions'].append(deepcopy(condition))
            if (condition.get('kind') == 'language' and condition.get('status') == 'contradicted'
                    and source.get('job_id') == packet['job_id'] and source.get('url') == packet['url']):
                for check in packet['language_comparisons']:
                    if check.get('quote') == source.get('quote'):
                        check.update(status=condition['status'], message=condition['message'],
                                     profile_facts=deepcopy(condition['profile_facts']))
    packet['decision_location_status'] = match.get('location_eligibility_status', 'unknown')
    packet['decision_source_location_checks'] = _bound_location_checks(match, packet)
    return packet


def _bound_location_checks(match, packet):
    """Retain executed checks only for this accepted body and exact source quote."""
    from wahojobs.candidate_source_display import plain
    from wahojobs.candidate_condition_comparisons import _condition_lines
    checks = []
    for check in match.get('source_task_location_checks') or []:
        if (not isinstance(check, dict) or not packet.get('source_hash')
                or check.get('job_id') != packet.get('job_id')
                or check.get('source_hash') != packet['source_hash']
                or check.get('status') not in ('eligible', 'incompatible', 'unknown', 'not_applicable')
                or not isinstance(check.get('quote'), str) or not check['quote']):
            continue
        quote, reference = check['quote'], check.get('source_reference')
        if reference == 'source location field':
            bound = any(plain(line).strip().lstrip('- ').strip() == quote
                        for line in (packet.get('text') or '').splitlines())
        else:
            bound = any(reference in (block.get('reference'), f"{block.get('reference')}:line {line}")
                        and plain(text).strip() == quote
                        for block in packet.get('blocks') or [] for line, text in _condition_lines(block))
        if bound:
            checks.append(deepcopy(check))
    return checks


def _joined(values):
    return values[0] if len(values) == 1 else ', '.join(values[:-1]) + ' and ' + values[-1]


def _activity_phrase(text):
    """Small grammatical display transform; the original fact stays in evidence."""
    first, separator, rest = text.partition(' ')
    inflections = {'review': 'reviewing', 'check': 'checking', 'evaluate': 'evaluating',
                   'assess': 'assessing', 'annotate': 'annotating', 'organize': 'organizing',
                   'answer': 'answering', 'compare': 'comparing', 'write': 'writing'}
    if first.casefold() in inflections and separator:
        return inflections[first.casefold()] + ' ' + rest
    return '“' + text + '”'


def _short_reason(packet, match):
    links = packet.get('transferable_task_links') or []
    if links:
        activities = list(dict.fromkeys(_activity_phrase(link['profile_fact']['text'])
                                       for link in links))[:2]
        return 'Your experience ' + _joined(activities) + ' is relevant to this work.'
    reasons = packet.get('decision_reasons') or []
    if not reasons:
        return ''
    accepted = match.get('accepted_task_fit') or {}
    if (same_source(accepted.get('source_reference') or {}, packet)
            and accepted.get('basis') in (None, 'confirmed_ai_work')
            and accepted.get('facts') and accepted.get('profile_facts')):
        return 'Your reported AI evaluation or annotation experience relates to these tasks.'
    return reasons[0]


def comparison_state(row):
    """Labels describe recorded states, without converting unknowns to failures."""
    if row['modality'] == 'not_required':
        return 'waived', 'Not required'
    if row['modality'] == 'preferred':
        return 'preferred', 'Employer preference'
    if (row['kind'] == 'unassessed' and row['modality'] == 'unspecified'
            and row['status'] != 'contradicted' and row['source']['heading'].casefold().rstrip(':') in (
            'engagement', 'role details', 'commitment', 'more about the opportunity')):
        return 'term', 'Employer term to review'
    if row['status'] == 'contradicted':
        return 'conflict', 'Conflicts with your profile'
    if row['modality'] in ('conflicting', 'unresolved'):
        return 'employer', 'Employer wording needs clarification'
    if row['status'] == 'supported':
        return 'supported', 'Supported by your profile'
    if row.get('supported_parts'):
        return 'partial', 'Partly supported by your profile'
    if row['status'] == 'not_established' and row['kind'] in ('education', 'tools', 'professional_background'):
        return 'missing', 'Relevant profile detail is not established'
    return 'uncompared', 'Comparison not established'


def _message(row):
    if comparison_state(row)[0] == 'term':
        return row['source']['quote']
    return row.get('message') or ''


def missing_language_fact(check):
    if check.get('modality') not in ('required', 'preferred') or check.get('status') in ('supported', 'contradicted'):
        return False
    facts = check.get('profile_facts') or []
    return any(not any(str(f.get('language', '')).casefold() == language.casefold()
        and f.get('proficiency') not in (None, '', 'unknown', 'unspecified') for f in facts)
        for language in check.get('languages') or [])


def _whole_language_support(row, packet, checks):
    """Reuse a complete executed language result, never a compound-clause part."""
    from wahojobs.matching.languages import LANGUAGE_ALIASES, normalize_language_text
    source = row.get('source') or {}
    if not packet.get('source_hash') or any(source.get(a) != packet.get(b) for a, b in (
            ('job_id', 'job_id'), ('external_id', 'external_id'),
            ('url', 'url'), ('source_hash', 'source_hash'))):
        return False
    phrase = re.fullmatch(r'(fluent|native)(?: in)? ([\w -]+?)(?: required)?\.?',
                          source.get('quote') or '', re.I)
    if phrase is None:
        return False
    language = LANGUAGE_ALIASES.get(normalize_language_text(phrase[2]))
    if language is None:
        return False
    return any(check.get('status') == 'supported' and check.get('modality') == 'required'
        and check.get('languages') == [language] and check.get('levels') == [phrase[1].casefold()]
        and check.get('operator') == 'all_of' and check.get('profile_facts')
        and check.get('quote') == source['quote'] and check.get('heading') == source.get('heading')
        and check.get('source_field') == f"{source.get('block_reference')}:line {source.get('line')}"
        and same_source(check.get('source_reference') or {}, packet) for check in checks)


def _row_state(row, packet):
    state = comparison_state(row)
    checks = [c for c in packet.get('language_comparisons') or [] if c.get('quote') == row['source']['quote']]
    if row['modality'] not in ('required', 'unspecified'):
        return state
    if any(c.get('status') == 'contradicted' and c.get('modality') == 'required' for c in checks):
        return 'conflict', 'Language requirement conflicts with your profile'
    if any(missing_language_fact(c) for c in checks):
        return 'missing', 'Language level is missing from your profile'
    if (row['kind'] == 'unassessed' and row['status'] == 'unresolved'
            and any(c.get('status') == 'supported' for c in checks)):
        if _whole_language_support(row, packet, checks):
            return 'supported', 'Supported by your stated language level'
        return 'partial', 'Language component supported by your profile'
    return state


def render_reasons(packet, *, heading_level=4):
    packet = packet or {}
    reason = packet.get('decision_short_reason') or next(iter(packet.get('decision_reasons') or []), '')
    if not reason:
        return ''
    return "<p class='decision-fit'>" + escape(reason) + '</p>'


def render_fit_support(packet):
    """Optional source/fact trace, separate from the scannable recommendation."""
    packet = packet or {}
    links = packet.get('transferable_task_links') or []
    if not links:
        return ''
    return ("<details class='decision-source candidate-support'><summary>About this recommendation</summary>"
        + ''.join('<p>Your confirmed activity: ' + escape(link['profile_fact']['text'])
            + '</p><p>Employer task:</p><blockquote>' + escape(link['quote'])
            + '</blockquote>' for link in links)
        + '<p>This uses your self-reported activities to identify related work; it does not establish prior professional AI work or satisfaction of every requirement.</p></details>')


def _condition_materiality(row, packet):
    from wahojobs.matching.recommendation_policy import condition_materiality
    for original, materiality in zip(packet.get('comparisons') or [], packet.get('condition_materialities') or []):
        if (original == row and materiality.get('source') == row.get('source')
                and row.get('kind') == 'unassessed' and row.get('status') == 'unresolved'
                and row.get('modality') in ('required', 'unspecified')):
            return materiality
    return condition_materiality(row, packet)


def material_warnings(packet):
    """Present material recorded comparisons; unknown never becomes a conflict."""
    if not packet:
        return []
    warnings = []
    for row in _ordered_rows(packet):
        # The existing matcher can record an explicit MUST as required beneath
        # an otherwise preferred heading. Keep the original comparison intact,
        # but do not hide that exact consumed material condition on its card.
        consumed = next((condition for condition in packet.get('decision_consumed_conditions') or []
                         if condition.get('source') == row.get('source')), None)
        if consumed is not None and consumed.get('modality') == 'required':
            # Detail can establish optional item experience that the earlier
            # matcher did not consume. Preserve this current comparison's
            # status and facts; only reuse the executed required modality.
            row = dict(row, modality='required')
        state, _ = _row_state(row, packet)
        if row.get('modality') in ('preferred', 'not_required') or state == 'supported':
            continue
        classification = _condition_materiality(row, packet)
        if classification['classification'] == 'generic_behavior_only':
            continue
        if state == 'term':
            continue  # Workload/pay facts remain in the fact grid and source.
        quote = row['source']['quote']
        if state == 'conflict':
            message = 'The requirement “' + quote + '” conflicts with your profile.'
        elif state in ('missing', 'partial', 'employer', 'uncompared'):
            message = 'Check the requirement: “' + quote + '”.'
        else:
            continue
        warnings.append(dict(kind='conflict' if state == 'conflict' else 'requirement', text=message,
            essential=(row.get('modality') == 'required' or row.get('kind') in
                       ('education', 'professional_background', 'language', 'workload'))))
    geography = packet.get('geography')
    source_locations = [check for check in packet.get('decision_source_location_checks') or []
                        if check['status'] in ('incompatible', 'unknown')]
    for check in source_locations:
        conflict = check['status'] == 'incompatible'
        warnings.append(dict(kind='conflict' if conflict else 'location', essential=True,
            text=('The employer’s location requirement conflicts with your profile: “' if conflict else
                  'Confirm the employer’s location requirement: “') + check['quote'] + '”.'))
    if geography and not source_locations:
        warnings.append(dict(kind='conflict' if packet.get('decision_location_status') == 'incompatible'
                             else 'location', text=geography, essential=True))
    published_location = (packet.get('location_context') or {}).get('published_field') or {}
    if (published_location.get('generic_country_tag') is False
            and published_location.get('source_field') == 'props.pageProps.job.location'
            and same_source(published_location, packet)
            and isinstance(published_location.get('value'), str)
            and re.search(r'\b(?:only|must|required|requirements?|eligible|eligibility|citizens?(?:hip)?|'
                          r'residents?|residence|residency|restricted|restriction|excluding|except|'
                          r'unavailable|not)\b', published_location['value'], re.I)):
        # The accepted field can contain a material restriction that the country
        # comparator cannot interpret. Keep its exact wording visible without
        # turning a page country tag into applicant eligibility or a new gate.
        # Plain city/area metadata remains available in the source disclosure;
        # it is not a material warning merely because it is not a country name.
        warnings.append(dict(kind='location', essential=True,
            text='Check the source’s location information: “' + published_location['value'] + '”.'))
    workload = packet.get('decision_profile_context', {}).get('workload') or {}
    if workload.get('guidance') and workload.get('state') in ('hard_conflict', 'hard_unresolved', 'soft_difference'):
        warnings.append(dict(kind='conflict' if workload['state'] == 'hard_conflict' else 'workload',
                             text=workload['guidance'], essential=True))
    # Do not repeat the task-placement narrative or already-presented language rows.
    excluded = {packet.get('task_fit_note'), geography, *(packet.get('language_notes') or [])}
    warnings.extend(dict(kind='source_term', text=note, essential=True) for note in packet.get('caveats') or []
                    if note and note not in excluded)
    for check in packet.get('language_comparisons') or []:
        if check.get('modality') != 'required' or check.get('status') == 'supported':
            continue
        if any(row['source']['quote'] == check.get('quote') for row in packet.get('comparisons') or []):
            continue
        conflict = check.get('status') == 'contradicted'
        warnings.append(dict(kind='conflict' if conflict else 'requirement', essential=True,
            text=('The language requirement conflicts with your profile: ' if conflict else
                  'Check the language requirement: ') + str(check.get('quote') or check.get('message') or '')))
    unique = []
    for warning in warnings:
        if warning not in unique:
            unique.append(warning)
    return sorted(unique, key=lambda warning: warning['kind'] != 'conflict')


def render_material_warnings(packet, *, compact=False):
    warnings = material_warnings(packet)
    if not warnings:
        return ''
    shown = [warning for index, warning in enumerate(warnings)
             if not compact or index < 2 or warning['kind'] == 'conflict' or warning.get('essential')]
    remaining = len(warnings) - len(shown)
    conflict = any(w['kind'] == 'conflict' for w in shown)
    return ("<aside class='decision-warning" + (' decision-warning-conflict' if conflict else '') + "'>"
        + ('<strong>Requirement conflict</strong>' if conflict else '<strong>Before applying</strong>' if compact else '')
        + ''.join('<p>' + escape(w['text']) + '</p>' for w in shown)
        + (f'<p>{remaining} more requirement' + ('s' if remaining != 1 else '')
           + ' to check in the job details.</p>' if remaining else '') + '</aside>')


def render_application_guidance(packet):
    """Bounded templates from existing source/profile evidence; no generated claims."""
    if not packet:
        return '<p>Read the employer’s requirements and confirm the terms before applying.</p>'
    if any(w['kind'] == 'conflict' for w in material_warnings(packet)):
        return '<p>Check the stated requirement with the employer before proceeding. Application wording does not resolve a conflicting requirement.</p>'
    sentences = []
    if packet.get('decision_has_reported_support'):
        sentences.append('Describe a genuine example of the relevant experience or skills in your profile.')
    elif packet.get('decision_short_reason') or packet.get('decision_reasons'):
        sentences.append('If you have relevant experience, describe a genuine example when applying.')
    generic = [row for row in packet.get('comparisons') or []
               if _condition_materiality(row, packet)['classification'] == 'generic_behavior_only']
    if generic:
        # A source request is advice to demonstrate something, never evidence that
        # the candidate already possesses it. Keep the employer's exact wording.
        quote = generic[0]['source']['quote']
        sentences.append('The employer asks for “' + quote + '”; use a genuine example if you have one.')
    workload = packet.get('decision_profile_context', {}).get('workload') or {}
    if workload.get('guidance') and workload.get('state') in ('preference', 'supported'):
        sentences.append(workload['guidance'])
    waivers = [row for row in packet.get('comparisons') or [] if row.get('modality') == 'not_required']
    if waivers:
        sentences.append('The employer states: “' + waivers[0]['source']['quote'] + '”.')
    if not sentences:
        sentences.append('Read the employer’s requirements and describe only experience you actually have.')
    return "<p class='application-guidance'>" + escape(' '.join(sentences)) + '</p>'


def _ordered_rows(packet):
    priority = {'conflict': 0, 'employer': 1, 'missing': 2, 'partial': 3,
                'uncompared': 4, 'supported': 5, 'preferred': 6, 'waived': 7, 'term': 8}
    return sorted(packet.get('comparisons') or [], key=lambda r: priority[_row_state(r, packet)[0]])


def render_assessment(packet, *, compact=False):
    if not packet:
        return ''
    rows = _ordered_rows(packet)
    if not rows:
        return ''
    if compact:
        # Conflicts remain visible regardless of the compact summary limit.
        rows = [r for r in rows if comparison_state(r)[0] != 'term']
        rows = [r for i, r in enumerate(rows) if i < 2 or _row_state(r, packet)[0] == 'conflict']
    parts = []
    seen_groups = set()
    for row in rows:
        group = row.get('requirement_group')
        if group and group in seen_groups:
            continue
        if group:
            seen_groups.add(group)
        state, label = _row_state(row, packet)
        source = row['source']
        routes = ((row.get('components') or {}).get('qualifying_routes') or {}).get('routes') or []
        if group and not routes:
            routes = [r['route_comparison'] for r in packet['comparisons']
                      if r.get('requirement_group') == group and r.get('route_comparison')]
        if routes:
            route_html = ''.join('<li><blockquote>' + escape(route['source']['quote']) + '</blockquote><p>'
                + escape(route['message']) + '</p></li>' for route in routes)
            parts.append("<li class='decision-point decision-" + state + "'><strong>Alternative qualifying routes</strong>"
                + '<p>' + escape(row['message']) + '</p>'
                + ("<details class='decision-source'><summary>Compare the employer’s alternative routes</summary>"
                   + '<ul>' + route_html + '</ul></details>' if compact else '<ul>' + route_html + '</ul>') + '</li>')
            continue
        facts = []
        for fact in row.get('profile_facts') or []:
            value = fact.get('value')
            if isinstance(value, str) and value and value not in facts:
                facts.append(value)
        language = ['Language component: ' + c['message'] for c in packet.get('language_comparisons') or []
                    if c.get('quote') == source['quote'] and c.get('message')]
        quote = ("<details class='decision-source'><summary>Profile evidence used</summary>"
            + "<p>Self-reported profile details: " + escape('; '.join(facts)) + '</p></details>') if facts and not compact else ''
        subject = source['quote']
        if compact and len(subject) > 170:
            subject = subject[:167].rstrip() + '…'
        parts.append("<li class='decision-point decision-" + state + "'><strong>" + label + '</strong>'
            + (" <span class='decision-modality'>Required</span>" if row['modality']=='required' else '')
            + ("<p class='decision-subject'>" + escape(subject) + '</p>' if state not in ('term',) else '')
            + ('<p>' + escape(_message(row)) + '</p>' if _message(row) else '')
            + ''.join('<p>' + escape(m) + '</p>' for m in dict.fromkeys(language)) + quote + '</li>')
    return ("<section class='decision-assessment' aria-label='Profile and requirement comparison'>"
        + ('' if compact else '<h2>How your profile compares</h2>')
        + ("<p class='candidate-note'>An unassessed qualification still needs review. This does not mean you lack it.</p>"
           if any(r['kind']=='unassessed' and comparison_state(r)[0]!='term' for r in rows) else '')
        + '<ul class="decision-points">' + ''.join(parts) + '</ul></section>')


def render_placement_summary(packet):
    placement = (packet or {}).get('placement_explanation')
    return ("<p class='decision-placement'>" + escape(placement['summary']) + '</p>') if placement else ''


def render_limits(packet):
    """One nonduplicated set of independent limits, including real conflicts."""
    if not packet:
        return ''
    # Task-fit conditions are explained in the comparison, with their recorded
    # placement cause above. Independent pay/location/language caveats remain.
    caveats = list(dict.fromkeys(c for c in packet.get('caveats') or [] if c
        and not (packet.get('placement_explanation') and c == packet.get('task_fit_note'))))
    return ("<ul class='candidate-caveats'>" + ''.join('<li>' + escape(c) + '</li>' for c in caveats)
            + '</ul>') if caveats else ''

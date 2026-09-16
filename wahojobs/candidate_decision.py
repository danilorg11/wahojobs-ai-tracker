"""Candidate presentation of existing evidence and decisions; never admission.

No new requirement interpretation, matching, persistence or authority lives here.
Source quotes and consulted profile facts retain their original provenance.
"""
from copy import deepcopy
from html import escape


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


def attach_decision(packet, match):
    """Project only explanations actually recorded by the current computation."""
    reasons = []
    accepted = match.get('accepted_task_fit') or {}
    packet['transferable_task_links'] = []
    if (same_source(accepted.get('source_reference') or {}, packet)
            and accepted.get('facts') and accepted.get('profile_facts')):
        if accepted.get('basis') == 'transferable_activity':
            packet['transferable_task_links'] = _transferable_links(accepted)
            reasons.extend('Your confirmed activity “' + link['profile_fact']['text']
                + '” can transfer to a related task in this opportunity.'
                for link in packet['transferable_task_links'])
        elif accepted.get('basis') in (None, 'confirmed_ai_work'):
            reasons.append('Your confirmed profile describes AI evaluation or annotation work that overlaps with the employer’s tasks.')
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
    packet['decision_reasons'] = list(dict.fromkeys(reasons))[:2]
    packet['language_comparisons'] = [deepcopy(c) for c in match.get('source_language_checks') or []
        if same_source(c.get('source_reference') or {}, packet)]
    reviewed = match.get('source_task_fit') or {}
    if same_source(reviewed.get('source_reference') or {}, packet):
        for condition in reviewed.get('conditions') or []:
            source = condition.get('source') or {}
            if (condition.get('kind') == 'language' and condition.get('status') == 'contradicted'
                    and source.get('job_id') == packet['job_id'] and source.get('url') == packet['url']):
                for check in packet['language_comparisons']:
                    if check.get('quote') == source.get('quote'):
                        check.update(status=condition['status'], message=condition['message'],
                                     profile_facts=deepcopy(condition['profile_facts']))
    packet['decision_location_status'] = match.get('location_eligibility_status', 'unknown')
    return packet


def comparison_state(row):
    """Labels describe recorded states, without converting unknowns to failures."""
    if row['modality'] == 'not_required':
        return 'waived', 'Not required'
    if row['modality'] == 'preferred':
        return 'preferred', 'Employer preference'
    if row['kind'] == 'unassessed' and row['source']['heading'].casefold().rstrip(':') in (
            'engagement', 'role details', 'commitment', 'more about the opportunity'):
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


def _row_state(row, packet):
    state = comparison_state(row)
    checks = [c for c in packet.get('language_comparisons') or [] if c.get('quote') == row['source']['quote']]
    if row['modality'] not in ('required', 'unspecified'):
        return state
    if any(c.get('status') == 'contradicted' and c.get('modality') == 'required' for c in checks):
        return 'conflict', 'Language requirement conflicts with your profile'
    if any(missing_language_fact(c) for c in checks):
        return 'missing', 'Language level is missing from your profile'
    if row['kind'] == 'unassessed' and any(c.get('status') == 'supported' for c in checks):
        return 'partial', 'Language component supported by your profile'
    return state


def render_reasons(packet, *, heading_level=4):
    reasons = (packet or {}).get('decision_reasons') or []
    if not reasons:
        return ''
    heading = 'h2' if heading_level == 2 else 'h4'
    links = packet.get('transferable_task_links') or []
    task_evidence = ("<details class='decision-source'><summary>How your activities relate to the work</summary>"
        + ''.join('<p>Your confirmed activity: ' + escape(link['profile_fact']['text'])
            + '</p><p>Employer task:</p><blockquote>' + escape(link['quote'])
            + '</blockquote>' for link in links)
        + '<p>This task overlap does not establish prior professional AI work or satisfy other requirements.</p></details>') if links else ''
    return ("<section class='decision-relevance'><" + heading + '>Why it may suit you</' + heading + '><ul>'
        + ''.join('<li>' + escape(reason) + '</li>' for reason in reasons)
        + '</ul>' + task_evidence
        + '<p class="candidate-note">Profile evidence is self-reported; it is not independent verification.</p></section>')


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

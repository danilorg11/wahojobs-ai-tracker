"""Source-established beginner access aligned with confirmed work interests.

This is relevance, not evidence of having performed a task. Source scope uses
the same complete-source safeguards as transferable activities; qualifications
remain with the existing late source-condition and preference consumers.
"""
from copy import deepcopy
import re


BASIS = 'beginner_interest'
REQUIREMENT = 'Beginner-accessible tasks aligned with stated interests'


def source_scope(source):
    """A beginner interest cannot replace a separate prior-work prerequisite."""
    from wahojobs.matching.transferable_tasks import source_scope as general_scope, candidate_directed_prerequisite, current_role_blocks
    from wahojobs.authenticated_card_evidence import _source_text, _QUALIFICATION_HEADINGS
    from wahojobs.candidate_condition_comparisons import _condition_lines, _modality
    scope = [proof for proof in general_scope(source)
             if proof.get('scope_kind') != 'any_field_transferable_only']
    if not scope:
        return []
    for block in current_role_blocks(_source_text(source)):
        heading = block['heading'].casefold().rstrip(':')
        for _, quote in _condition_lines(block):
            for clause in re.split(r'(?<=[.!?;])\s+|[—–]', re.sub(r'[*#]', '', quote)):
                mode = _modality(heading, clause.strip())
                if mode in ('preferred', 'not_required'):
                    continue
                directed_need = candidate_directed_prerequisite(clause)
                if (heading in _QUALIFICATION_HEADINGS or directed_need or
                        re.search(r'\b(?:must|required|requires?|necessary|prerequisite)\b', clause, re.I)):
                    if re.search(r'\b(?:experience|experienced|prior work|previous work|track record|'
                                 r'(?:prior|previous) employment|(?:work|employment) history|'
                                 r'history of (?:paid |professional )?employment)\b', clause, re.I):
                        return []
    return scope


def interest_families(value):
    """Bounded existing work-interest vocabulary, never a summary/skill guess."""
    if not isinstance(value, str):
        return ()
    text = re.sub(r'\s+', ' ', value.strip().casefold()).replace('-', ' ')
    if text in ('ai evaluation', 'ai evaluation work', 'model evaluation',
                'ai response evaluation', 'ai content review'):
        return ('ai_evaluation',)
    if text in ('data annotation', 'annotation', 'data labeling', 'data labelling'):
        return ('data_annotation',)
    if text in ('ai training', 'ai training work'):
        return ('ai_evaluation', 'data_annotation')
    return ()


def confirmed_interests(canonical):
    # The existing durable-to-matcher adapter deliberately represents reviewed
    # values as explicit external_import. That is provisional projection, not
    # upgraded confirmation; bind_confirmed_profile must consult durable refs
    # before admission/presentation, as it does for transferable activities.
    if canonical.get('provenance', {}).get('reviewed') is not True:
        return []
    refs = canonical.get('provenance', {}).get('field_sources', {})
    result = []
    for index, value in enumerate(canonical.get('preferences', {}).get('target_opportunity_types', [])):
        path = f'preferences.target_opportunity_types[{index}]'
        ref = refs.get(path) if isinstance(refs, dict) else None
        families = interest_families(value)
        if (families and isinstance(ref, dict) and ref.get('explicit') is True
                and ref.get('source') in ('user_confirmation', 'user_correction', 'external_import')):
            result.append(dict(path=path, text=value, provenance=deepcopy(ref), task_families=list(families)))
    return result


def duty_family(fact):
    # These are already assigned accepted duties, not requirements/marketing.
    # Reuse the existing action/object parser rather than introduce a new task
    # taxonomy or turn every generalist title into a relevant duty.
    from wahojobs.matching.accepted_tasks import _task_action, _EVALUATE, _ANNOTATE
    if fact.get('professional_domains'):
        return None
    action = _task_action(fact.get('quote', ''), ai_context=True)
    if action and re.fullmatch(_EVALUATE, action.group(), re.I):
        return 'ai_evaluation'
    if action and re.fullmatch(_ANNOTATE, action.group(), re.I):
        return 'data_annotation'
    return None


def match_interests(profile, evidence):
    interests = profile.get('confirmed_beginner_interest_evidence') or []
    scope = evidence.get('beginner_scope') or []
    if not scope or not interests:
        return None
    links = []
    for fact in evidence.get('facts') or []:
        family = duty_family(fact)
        for interest in interests:
            if family and family in interest.get('task_families', []):
                links.append(dict(quote=fact['quote'], block_reference=fact['block_reference'],
                    profile_fact=deepcopy(interest), task_family=family, support_kind=BASIS))
    if not links:
        return None
    facts = [f for f in evidence['facts'] if any(l['quote'] == f['quote'] and l['block_reference'] == f['block_reference'] for l in links)]
    profile_facts = [f for f in interests if any(l['profile_fact']['path'] == f['path'] for l in links)]
    from wahojobs.matching.transferable_tasks import match_activities
    related = match_activities(profile, evidence)
    result = dict(basis=BASIS, source_reference=deepcopy(evidence['source_reference']),
        facts=deepcopy(facts), profile_facts=deepcopy(profile_facts),
        interest_links=links, scope_evidence=deepcopy(scope))
    if related:
        result['related_activity_fit'] = related
    return result


def bind_confirmed_profile(task_fit, profile):
    """The current durable confirmed interest must match its projected path/text."""
    from wahojobs.professional_background_duration import confirmed_fact
    bound = deepcopy(task_fit)
    for link in bound.get('interest_links', []):
        fact = link['profile_fact']
        found = re.fullmatch(r'preferences\.target_opportunity_types\[(\d+)\]', fact['path'])
        values = profile.get('preferences', {}).get('target_opportunity_types', [])
        if not found or int(found[1]) >= len(values) or values[int(found[1])] != fact['text']:
            return None
        authority = confirmed_fact(profile, fact['path'], fact['text'])
        if authority is None or link.get('task_family') not in interest_families(fact['text']):
            return None
        fact['provenance'] = authority['sources']
    for fact in bound['profile_facts']:
        fact['provenance'] = next(l['profile_fact']['provenance'] for l in bound['interest_links'] if l['profile_fact']['path'] == fact['path'])
    if bound.get('related_activity_fit'):
        from wahojobs.matching.transferable_tasks import bind_confirmed_profile as bind_activity
        related = bind_activity(bound['related_activity_fit'], profile)
        if related is None:
            bound.pop('related_activity_fit')
        else:
            bound['related_activity_fit'] = related
    return bound if bound.get('interest_links') else None

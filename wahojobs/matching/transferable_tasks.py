"""Bounded, derived activity relevance for accepted entry-level source duties.

This never creates professional AI history, a skill, a duration or a score.
Activities retain their confirmed path/text; source scope and duties retain quotes.
"""
from copy import deepcopy
import re


BASIS = 'transferable_activity'
REQUIREMENT = 'Transferable activity for entry-level tasks'
_ACTION = r'(?:review|evaluat|assess|compar|check|verif|validat)\w*'
_OBJECT = r'(?:written\s+)?(?:responses?|answers?|text|content|documents?|information|facts?)'
_UNOWNED = re.compile(r"\b(?:not|never|no|without|interested|interest|want|wish|hope|plan|would|could|will|learn|learning|their|his|her|friend|colleague|said|says|reported|example)\b", re.I)


def activity_families(text, *, source=False):
    """An action/object relationship, not a bag of generic positive keywords."""
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        return ()
    plain = re.sub(r'[*#]', '', text).strip().lstrip('- ')
    if not source:
        if _UNOWNED.search(plain) or not re.match(r'^(?:I\s+)?' + _ACTION + r'\b', plain, re.I):
            return ()
    from wahojobs.profiles.normalizer import term_is_negated
    families = []
    for action in re.finditer(r'\b' + _ACTION + r'\b', plain, re.I):
        # Adjacency is intentional: a subordinate clause or a different work
        # object cannot lend its later text/content noun to the first action.
        obj = re.match(r'(?:\s*(?:,|and)\s+' + _ACTION + r')*\s+'
            r'(?:(?:the|written|textual)\s+|(?:AI|LLM|model)[ -](?:generated|produced)\s+)*'
            + _OBJECT + r'\b', plain[action.end():], re.I)
        if obj and not term_is_negated(plain.lower(), action.start(), action.end()):
            families.append('written_content_review')
            if re.search(r'\b(?:against|following|using)\s+(?:the\s+|provided\s+|written\s+|detailed\s+)?(?:instructions?|guidelines?|rubrics?|rules?)\b', plain, re.I):
                families.append('instruction_based_checking')
    return tuple(dict.fromkeys(families))


def confirmed_activities(canonical):
    if canonical.get('provenance', {}).get('reviewed') is not True:
        return []
    refs = canonical.get('provenance', {}).get('field_sources', {})
    result = []
    for index, value in enumerate(canonical.get('experience', {}).get('specialties', [])):
        path = f'experience.specialties[{index}]'
        ref = refs.get(path) if isinstance(refs, dict) else None
        families = activity_families(value)
        if families and isinstance(ref, dict) and ref.get('explicit') is True:
            result.append(dict(path=path, text=value, provenance=deepcopy(ref), task_families=list(families)))
    return result


def source_scope(source):
    """Positive body-level entry scope, with separate central-prerequisite vetoes.

    No title, provider, job ID or candidate participates in this determination.
    A no-AI waiver alone never proves non-specialist scope. Unknown scope stays
    unavailable; preferred qualifications do not become mandatory restrictions.
    """
    from wahojobs.authenticated_card_evidence import _blocks, _source_text, _QUALIFICATION_HEADINGS
    from wahojobs.candidate_condition_comparisons import _condition_lines, _modality
    from scripts.profile_match_digest import detect_role_match_features
    from wahojobs.matching.source_task_fit import _task
    text = _source_text(source)
    blocks = _blocks(text)
    # Established specialist linguistic/teaching duties remain their own route.
    if _task(text) is not None:
        return []
    scope = []
    for block in blocks:
        heading = block['heading'].casefold().rstrip(':')
        for number, quote in _condition_lines(block):
            plain = re.sub(r'[*#]', '', quote)
            # A paragraph can contain an absolute waiver and an independent
            # mandatory prerequisite. Bind each decision to its own clause;
            # retain the complete original quote in the evidence packet.
            clauses = re.split(r'(?<=[.!?;])\s+|[—–]', plain)
            for clause in clauses:
                clause = clause.strip()
                entry_proof = False
                # These are current-posting statements, not company history,
                # another team's opening, a quoted example or an aspiration.
                if (not re.search(r'\b(?:but|unless|except|however|if|no longer|used to|formerly|previously|hope|wish|future|will|would|could|may|might|intend|planned|not|never)\b', clause, re.I)
                        and (re.match(r'^no (?:specialized|specialist|professional) background(?: or prior AI experience)? (?:is )?(?:required|needed|necessary)\b', clause, re.I)
                             or re.fullmatch(r'(?:(?:this is|(?:this|the) (?:role|position|opportunity|job) is) (?:an? )?|an? )?entry[- ]level (?:role|opportunity|work|position|task)(?: (?:for|open to) (?:beginners|first[- ]time workers))?[.!]?', clause, re.I)
                             or re.match(r'^(?:(?:this|the) (?:role|position|opportunity|job) is )?open to (?:beginners|first[- ]time workers)\b', clause, re.I))):
                    proof = dict(quote=quote, block_reference=block['reference'], line=number,
                                  scope_kind='explicit_entry_level_or_non_specialized')
                    entry_proof = True
                    if proof not in scope:
                        scope.append(proof)
                mode = _modality(heading, clause)
                if mode in ('preferred', 'not_required'):
                    continue
                explicit_prerequisite = bool(re.search(r'\b(?:must|required|requires?|necessary|prerequisite)\b', clause, re.I))
                if heading not in _QUALIFICATION_HEADINGS and not explicit_prerequisite and not entry_proof:
                    continue
                # Exact central prerequisites are not waived by entry wording.
                # Existing source comparators still examine remaining clauses.
                if (detect_role_match_features(clause)['professional_domains']
                        or re.search(r'\b(?:licen[cs]ed?|certified|doctorate|doctoral|Ph\.?D|clinical|linguistic analysis|professional experience|subject[- ]matter expertise)\b', clause, re.I)
                        or re.search(r'\b(?:AI|LLM|model evaluation|annotation)\b[^.;]{0,45}\b(?:experience|work|background)\b|\b(?:experience|work|background)\s+(?:in|with|of)\s+(?:AI|LLM|model evaluation|annotation|Python|programming|software)\b', clause, re.I)):
                    return []
    return scope


def match_activities(profile, evidence):
    activities = profile.get('confirmed_transferable_activity_evidence') or []
    scope = evidence.get('transferable_scope') or []
    if not activities or not scope:
        return None
    links = []
    for fact in evidence.get('facts') or []:
        if fact.get('professional_domains'):
            continue
        families = set(activity_families(fact['quote'], source=True))
        for activity in activities:
            for family in sorted(families.intersection(activity['task_families'])):
                links.append(dict(quote=fact['quote'], block_reference=fact['block_reference'],
                    profile_fact=deepcopy(activity), task_family=family, support_kind=BASIS))
    if not links:
        return None
    facts = [f for f in evidence['facts'] if any(l['quote'] == f['quote'] and l['block_reference'] == f['block_reference'] for l in links)]
    profile_facts = [f for f in activities if any(l['profile_fact']['path'] == f['path'] for l in links)]
    return dict(basis=BASIS, source_reference=deepcopy(evidence['source_reference']),
                facts=deepcopy(facts), profile_facts=deepcopy(profile_facts), task_links=links,
                scope_evidence=deepcopy(scope))


def bind_confirmed_profile(task_fit, profile):
    """Late review rebinds to durable confirmed facts; no projected shadow wins."""
    from wahojobs.professional_background_duration import confirmed_fact
    bound = deepcopy(task_fit)
    for link in bound.get('task_links', []):
        fact = link['profile_fact']
        found = re.fullmatch(r'experience\.specialties\[(\d+)\]', fact['path'])
        values = profile.get('experience', {}).get('specialties', [])
        if not found or int(found[1]) >= len(values) or values[int(found[1])] != fact['text']:
            return None
        authority = confirmed_fact(profile, fact['path'], fact['text'])
        if authority is None:
            return None
        fact['provenance'] = authority['sources']
    for fact in bound['profile_facts']:
        fact['provenance'] = next(l['profile_fact']['provenance'] for l in bound['task_links'] if l['profile_fact']['path'] == fact['path'])
    return bound if bound.get('task_links') else None

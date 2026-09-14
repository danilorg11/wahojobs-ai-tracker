"""Bounded comparisons for an already-selected exact-source packet.

Only the small clause forms below are compared; this is not a general parser
or an eligibility policy. All other wording remains unassessed. Results
are ephemeral and retain exact source and current profile references.
"""
from copy import deepcopy
from html import escape
import re

from wahojobs.profiles.canonical_v2 import normalize_comparison_label
from wahojobs.profiles.education_entries import MAX_EDUCATION_ENTRY_TEXT_CHARS
from wahojobs.profiles.preference_model import WORKLOADS


_LEVEL = r"Ph\.?D\.?|doctorate|doctoral candidate|Master['’]s(?: degree)?|Bachelor['’]s(?: degree)?"
_TOOLS = r'Python|R|GitHub|Git|Docker|another relevant programming language'
_REQUIRED = {'required', 'requirements', 'required qualifications', 'minimum qualifications',
             'required skills and qualifications', 'requirements (must have)'}
_PREFERRED = {'preferred', 'preferred qualifications', 'ideal qualifications', 'nice to have',
              'preferred (nice to have)'}


def _fact(profile, path, value):
    refs = profile.get('provenance', {}).get('field_sources', [])
    if isinstance(refs, dict):
        # Legacy comparison callers carry a path->source map. Retain its
        # recorded provenance for explanation; do not invent durable source
        # ordinals/path versions or upgrade it to canonical confirmation.
        refs = [dict(ref, field_path=key) for key, ref in refs.items() if isinstance(ref, dict)]
    return {'field_path': path, 'value': deepcopy(value), 'sources': deepcopy([
        ref for ref in refs if isinstance(ref, dict)
        and (ref.get('field_path') == path or ref.get('field_path', '').startswith((path + '[', path + '.')))])}


def _explicit(fact):
    return any(ref.get('explicit') is True for ref in fact['sources'])


def _waiver_modality(quote):
    """Recognize a complete explicit waiver, never waive a neighboring clause.

    This is source modality, not positive candidate evidence. Compound exceptions
    stay unresolved; a trailing 'required' cannot reverse their negation.
    """
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', quote).strip().rstrip('.')
    if re.match(r'No (?:less|fewer|more) than\b', text, re.I):
        return None  # a quantitative bound, not a waiver
    waived = re.fullmatch(r'No (.+?) (?:is |are )?(?:required|needed|necessary)', text, re.I)
    if not waived:
        waived = re.fullmatch(r'(.+?) (?:is |are )?not (?:required|needed|necessary)', text, re.I)
    if waived and not re.search(
            r'[.;:!?—–]|\b(?:not|no|without|but|however|unless|except|if|only|must|less|fewer|more|at least|requires?|required|needed|necessary)\b',
            waived[1], re.I):
        return 'not_required'
    if re.search(r'\bnot (?:required|needed|necessary)\b|\bno .+?\b(?:required|needed|necessary)\b', text, re.I):
        return 'unresolved'
    return None


def _modality(heading, quote):
    waiver = _waiver_modality(quote)
    if waiver:
        return waiver
    label = heading.casefold().rstrip(':')
    mode = 'required' if label in _REQUIRED else 'preferred' if label in _PREFERRED else 'unspecified'
    inline = re.search(r'\b(required|preferred)\.?$', quote, re.I)
    if inline:
        if mode != 'unspecified' and mode != inline[1].lower():
            return 'conflicting'
        mode = inline[1].lower()
    return mode


def _lines(block):
    # Join wrapped bullets, not separate alternatives or paragraphs.
    current, start = '', 0
    for number, line in enumerate(block['text'].splitlines(), 1):
        if not line.strip():
            if current:
                yield start, current
            current = ''
        elif re.match(r'^\s*(?:[-*+]|\d+[.)])\s+', line) or not current:
            if current:
                yield start, current
            current, start = re.sub(r'^\s*(?:[-*+]|\d+[.)])\s+', '', line).strip(), number
        else:
            current += ' ' + line.strip()
    if current:
        yield start, current


def _education(quote, profile, *, generic_degree=False):
    from wahojobs.professional_background_duration import confirmed_fact
    pattern = 'degree' if generic_degree else _LEVEL
    match = re.fullmatch(rf'({pattern})(?: or ({pattern}))? in (.+?)\.?', quote, re.I)
    if not match:
        return None
    fields = match[3]
    if ',' in fields and not re.search(r'\bor\b', fields, re.I):
        return None
    # Do not absorb additional conditions, negation, institutions or degree ANDs.
    if re.search(r'\b(?:and|with|from|without|not|unless|if|including|equivalent|experience|years)\b|[;:()]', fields, re.I):
        return None
    alternatives = [part.strip() for part in re.split(r',\s*(?:or\s+)?|\s+or\s+', fields)]
    related = [f for f in alternatives if re.fullmatch(r'(?:a )?closely related field', f, re.I)]
    exact_fields = [f for f in alternatives if f not in related]
    if not exact_fields or any(len(f) > MAX_EDUCATION_ENTRY_TEXT_CHARS or
                               not re.fullmatch(r'[\w &/-]+', f) for f in exact_fields):
        return None
    def level(label):
        if label.lower().startswith('master'): return ('master', 'completed')
        if label.lower().startswith('bachelor'): return ('bachelor', 'completed')
        return ('doctorate', 'in_progress' if label.lower() == 'doctoral candidate' else 'completed')
    levels = ([(kind, 'completed') for kind in ('associate', 'bachelor', 'master', 'doctorate', 'professional_degree')]
              if generic_degree else [level(s) for s in match.groups()[:2] if s])
    facts, matching, degree_only = [], [], []
    for i, entry in enumerate(profile.get('education', {}).get('entries', [])):
        facts.extend(_fact(profile, f'education.entries[{i}].{key}', entry.get(key))
                     for key in ('kind', 'qualification', 'field', 'status'))
        if generic_degree:
            if not all(confirmed_fact(profile, f'education.entries[{i}].{key}', entry.get(key))
                       for key in ('kind', 'field', 'status')):
                continue
        if (entry.get('kind'), entry.get('status')) in levels:
            degree_only.append(entry)
            if normalize_comparison_label(entry.get('field', '')) in map(normalize_comparison_label, exact_fields):
                matching.append(entry)
    level_fact = _fact(profile, 'education.education_level', profile.get('education', {}).get('education_level'))
    facts.append(level_fact)
    if (level_fact['value'] == 'no_degree' and _explicit(level_fact)
            and (not generic_degree or confirmed_fact(profile, 'education.education_level', 'no_degree'))):
        if degree_only:
            return 'unresolved', 'Your education entries conflict. Confirm your degree before relying on this comparison.', facts, []
        return 'contradicted', 'This asks for a degree; your profile explicitly says you have no degree.', facts, []
    if matching:
        entry = matching[0]
        label = entry.get('qualification') or f"{entry['kind']} in {entry['field']}"
        return 'supported', f'Your {label} matches this education option.', facts, ['degree level/status', 'explicit field option']
    if degree_only:
        entry = degree_only[0]
        field = entry.get('field') or 'your field'
        return ('unresolved', f"Your degree level is listed. Confirm whether {field} is accepted for this field requirement.",
                facts, ['degree level/status'])
    return 'not_established', 'The requested degree and field aren’t established in your profile.', facts, []


def _tool_groups(text):
    """A few explicit list forms; mixed ungrouped AND/OR and slash stay unknown."""
    text = re.sub(r' for scientific computing$', '', text, flags=re.I)
    text = re.split(r' [—–] ', text, maxsplit=1)[0].rstrip('.')
    text = text.replace('running code in ', '')
    if '/' in text or (' and ' in text and ' or ' in text):
        # Recognize this captured tool-only conjunction without resolving slash.
        if re.fullmatch(r'Git/GitHub and Docker', text, re.I):
            return [['Git', 'GitHub'], ['Docker']], 'unresolved_slash'
        return None
    operator = 'any_of' if re.search(r'\bor\b', text) else 'all_of'
    parts = re.split(r',\s*(?:(?:or|and)\s+)?|\s+(?:or|and)\s+', text)
    if not parts or any(not re.fullmatch(_TOOLS, part.strip(), re.I) for part in parts):
        return None
    # An unqualified comma list has no reliably established logical operator.
    if ',' in text and not re.search(r'\b(?:or|and)\b', text):
        return None
    return ([parts] if operator == 'any_of' else [[p] for p in parts]), operator


def _tools(quote, profile, *, include_item_experience=False):
    match = re.fullmatch(r'(Working proficiency|Proficiency|Experience|Comfortable) (?:in|with) (.+)', quote, re.I)
    parsed = _tool_groups(match[2]) if match else None
    if not parsed:
        return None
    groups, operator = parsed
    candidates = {token.casefold(): token for group in groups for token in group
                  if token.casefold() != 'another relevant programming language'}
    facts, mentioned, denied = [], set(), set()
    skills = profile.get('skills', {})
    for key in ('normalized', 'free_text_labels', 'technical', 'software_tools', 'domain_specific'):
        for i, value in enumerate(skills.get(key, [])):
            if isinstance(value, str) and value.casefold() in candidates:
                mentioned.add(value.casefold()); facts.append(_fact(profile, f'skills.{key}[{i}]', value))
    for i, entry in enumerate(skills.get('entries', [])):
        value = entry.get('skill', '')
        if value.casefold() in candidates:
            mentioned.add(value.casefold()); facts.append(_fact(profile, f'skills.entries[{i}].skill', value))
    # Only explicit negative experience statements, never avoidance/preferences.
    for key in ('hard_constraints', 'negative_constraints'):
        for i, value in enumerate(profile.get('constraints', {}).get(key, [])):
            negative = re.fullmatch(rf'(?:I have )?no experience with ({_TOOLS})\.?', value, re.I)
            if negative and negative[1].casefold() in candidates:
                fact = _fact(profile, f'constraints.{key}[{i}]', value); facts.append(fact)
                if _explicit(fact): denied.add(negative[1].casefold())
    # Record the consulted collections as well, so absence is traceable.
    if not mentioned:
        facts.append(_fact(profile, 'skills', skills))
    if mentioned & denied:
        return 'unresolved', 'Your profile gives conflicting tool-experience information. Confirm it before applying.', facts, []
    failed_group = any(all(t.casefold() in denied for t in group) for group in groups)
    if failed_group and operator != 'unresolved_slash':
        names = ', '.join(candidates[t] for t in sorted(denied))
        return 'contradicted', f'The source asks for experience; your profile explicitly states no experience with {names}.', facts, []
    if include_item_experience:
        # Compare only the already-supported clause forms. Duration, expertise,
        # job-specific autonomy and specialized domains are not inferred here.
        from wahojobs.profiles.item_experience import linked, PREFIX
        scoped_wording = bool(re.search(r'\bfor\b| [—–] ', match[2], re.I))
        reported, usable, conflicting = set(), set(), False
        for i, detail in enumerate(profile.get('experience', {}).get('item_details', [])):
            tool = detail['label'].casefold()
            if tool not in candidates or not linked(profile, detail):
                continue
            fact = _fact(profile, f'{PREFIX}[{i}]', detail)
            if detail.get('basis') != 'self_reported' or not fact['sources'] or not all(
                    s.get('explicit') is True and s.get('source_kind') in ('user_confirmation', 'user_correction')
                    for s in fact['sources']):
                continue
            facts.append(fact)
            if detail['contexts']:
                reported.add(tool)
                if not scoped_wording and match[1].casefold() == 'experience': usable.add(tool)
                if not scoped_wording and match[1].casefold() == 'working proficiency' and detail['autonomy'] in ('independent', 'complex'):
                    usable.add(tool)
            if tool in denied and (detail['contexts'] or detail['autonomy'] != 'unknown'):
                conflicting = True
        if conflicting:
            return 'unresolved', 'Your profile gives conflicting tool-experience information. Confirm it before applying.', facts, []
        if operator != 'unresolved_slash' and all(any(t.casefold() in usable for t in group) for group in groups):
            names = ', '.join(candidates[t] for t in sorted(usable))
            basis = 'use' if match[1].casefold() == 'experience' else 'independent routine use'
            return 'supported', f'You report {basis} of {names}, supporting this tool condition. This is your own assessment, not independently verified.', facts, ['self-reported tool experience: ' + candidates[t] for t in sorted(usable)]
        if reported:
            names = ', '.join(candidates[t] for t in sorted(reported))
            if scoped_wording:
                return 'not_established', f'You report using {names}. The specific use or additional qualifications in this source clause still need confirmation.', facts, ['self-reported use: ' + candidates[t] for t in sorted(reported)]
            return 'not_established', f'You report using {names}. The requested proficiency or remaining tool requirements still aren’t established.', facts, ['self-reported use: ' + candidates[t] for t in sorted(reported)]
    if mentioned:
        names = ', '.join(candidates[t] for t in sorted(mentioned))
        message = f'Your profile mentions {names}, an accepted tool option.' if operator == 'any_of' else f'Your profile mentions {names}.'
        missing = [t for t in candidates if t not in mentioned]
        if operator == 'all_of' and missing:
            message += ' ' + ', '.join(candidates[t] for t in missing) + ' isn’t listed.'
        message += ' Confirm the requested proficiency and usage; a tool mention doesn’t establish them.'
        if operator == 'unresolved_slash':
            message += ' Confirm how the employer treats Git/GitHub.'
        return 'not_established', message, facts, ['tool mention: ' + candidates[t] for t in sorted(mentioned)]
    names = 'Git/GitHub and Docker' if operator == 'unresolved_slash' else (' or ' if operator == 'any_of' else ' and ').join(candidates.values())
    if any(t.casefold() == 'another relevant programming language' for group in groups for t in group):
        return 'not_established', f'Confirm the requested proficiency in {names} or another relevant programming language.', facts, []
    return 'not_established', f'{names} experience isn’t stated in your profile. Confirm the requested proficiency.', facts, []


def _workload(quote, profile):
    match = re.fullmatch(r'(?:Expected )?Commitment:\s*((?:part-time, )?\d+(?:[-–]\d+)?\+? hours(?: per |/)week)\.?', quote, re.I)
    if not match:
        return None
    preferences = profile.get('preferences', {})
    # Use the authoritative typed preference projection; this is a soft wish,
    # never evidence of actual availability or an inferred hourly capacity.
    workloads = preferences.get('preference_model', {}).get('workloads', [])
    facts = [_fact(profile, 'preferences.preference_model.workloads', workloads),
             _fact(profile, 'preferences.availability', preferences.get('availability', 'unknown'))]
    wanted = [w.replace('_', '-') for w in workloads if w in WORKLOADS]
    prefix = f"You prefer {' or '.join(wanted)} work. " if wanted else ''
    availability = facts[1]
    if availability['value'] == 'unavailable' and _explicit(availability):
        return 'contradicted', f"Your profile says you’re unavailable; this asks for {match[1]}. Confirm whether that has changed.", facts, []
    return ('not_established', prefix + f"Confirm you can commit to {match[1]}; your available hours aren’t established.", facts, [])


def _professional_background(quote, profile, *, include_item_experience=False):
    """Compare a few explicit background forms, without a profession taxonomy.

    The alternatives come from the source, not the title. Study/skill mentions
    remain distinct from roles/practice and never prove hands-on work or years.
    Generic annotation of a domain does not establish that domain's profession.
    """
    text = quote.strip().rstrip('.')
    if re.search(r'\b(?:not|no|unless|except|if|preferred|ideal|optional)\b', text, re.I):
        return None
    from wahojobs.professional_background_duration import requirement, compare_duration
    duration = requirement(text)
    if duration:
        # Compare the recorded facts without inventing profession aliases or
        # resolving compound domain wording. Total career years are not years
        # in the required practice; even an exact duration is not competence.
        experience = profile.get('experience', {})
        facts = [_fact(profile, 'experience.' + key, experience.get(key)) for key in
                 ('recent_roles', 'job_titles', 'total_years', 'years_by_domain')]
        compared = _professional_background('hands-on ' + duration['scope'] + ' experience', profile,
                                            include_item_experience=include_item_experience)
        if compared:
            facts += compared[2]
        # An explicit denial of the entire named field remains a contradiction
        # even when its internal slash relationship cannot be interpreted.
        denials = []
        for key in ('hard_constraints', 'negative_constraints'):
            for i, value in enumerate(profile.get('constraints', {}).get(key, [])):
                denied = re.fullmatch(r'(?:I have )?no (?:hands-on |professional )?experience (?:in|with) (.+?)\.?', value, re.I)
                fact = _fact(profile, f'constraints.{key}[{i}]', value)
                if denied and normalize_comparison_label(denied[1]) == normalize_comparison_label(duration['scope']) and _explicit(fact):
                    denials.append(fact)
        years = [item for item in experience.get('years_by_domain', [])
                 if isinstance(item, dict) and normalize_comparison_label(item.get('domain', '')) ==
                    normalize_comparison_label(duration['scope'])]
        total = experience.get('total_years')
        message = (f'The source requires {duration["minimum"]}+ relevant professional {duration["unit"]} in '
                   f'{duration["scope"]}. ')
        if total is not None:
            message += f'Your {total} total career years do not establish that domain-specific duration. '
        if experience.get('recent_roles') or experience.get('job_titles'):
            message += 'Your recorded roles are supplied for comparison; their scope and relevant duration still need confirmation. '
        if years:
            message += 'Domain-year entries are supplied separately; confirm whether they cover the requested professional practice. '
        if duration['operator'] == 'unresolved':
            message += 'The relationship between the named source domains remains unresolved.'
        checked_duration = compare_duration(text, profile)
        if checked_duration['status'] == 'contradicted':
            duration_facts = [f for b in checked_duration['bounds'] for f in b['profile_facts']]
            return ('contradicted', checked_duration['message'] + ' ' + message.strip(),
                    facts + duration_facts, compared[3] if compared else [])
        if denials and compared is None or compared and compared[0] == 'contradicted':
            return 'contradicted', 'Your confirmed profile denies the professional background required by this clause. ' + message.strip(), facts + denials, []
        if compared:
            return compared[0], compared[1] + ' ' + message.strip(), facts, compared[3]
        return 'unresolved' if experience.get('recent_roles') or experience.get('job_titles') or years else 'not_established', message.strip(), facts, []
    patterns = (
        r'(?:\d+\+? years of )?hands-on (?P<fields>.+?) experience',
        r'Hands-on experience in (?:a |an )?(?P<fields>.+?) role(?: [—–] .+)?',
        r'Hands-on experience in (?P<fields>.+?) at (?:an? )?.+? (?:company|organization|firm)',
        r'Experienced (?P<fields>.+?) with hands-on .+? experience',
    )
    match = next((m for pattern in patterns if (m := re.fullmatch(pattern, text, re.I))), None)
    if not match:
        return None
    raw = match['fields']
    if re.search(r'\band\b|/|[():;]', raw, re.I) or (',' in raw and not re.search(r'\bor\b', raw, re.I)):
        return None  # do not flatten ambiguous or cumulative requirements
    alternatives = [x.strip().casefold() for x in re.split(r',\s*(?:or\s+)?|\s+or\s+', raw)]
    if any(not re.fullmatch(r'[\w -]{2,80}', x) for x in alternatives):
        return None
    from wahojobs.profiles.normalizer import term_is_negated
    paths = [('experience.'+key, profile.get('experience',{}).get(key,[])) for key in
             ('recent_roles','job_titles','professional_domains','occupational_families','specialties')]
    paths += [('skills.'+key, profile.get('skills',{}).get(key,[])) for key in
              ('domain_specific','normalized','free_text_labels')]
    paths += [('education.fields_or_domains',profile.get('education',{}).get('fields_or_domains',[]))]
    paths += [(f'education.entries[{i}].field',[entry.get('field','')])
              for i,entry in enumerate(profile.get('education',{}).get('entries',[]))]
    facts, related, practice, denied = [], set(), set(), set()
    for path, values in paths:
        for i,value in enumerate(values):
            if not isinstance(value,str) or re.search(r'\b(?:interested in|want to|hope to|would like|aspir)\w*',value,re.I):
                continue
            for option in alternatives:
                hits = list(re.finditer(r'(?<!\w)'+re.escape(option)+r's?(?!\w)',value,re.I))
                if not any(not term_is_negated(value.lower(),m.start(),m.end()) for m in hits):
                    continue
                # A generic evaluation/annotation label cannot turn 'audio'
                # into hands-on professional audio experience, for example.
                if len(option.split()) == 1 and re.search(r'\b(?:AI|LLM|model|evaluation|annotation|review|labeling)\b',value,re.I):
                    continue
                # The collection matters: a marketing skill or degree is not
                # a marketing role. Even a role label states no depth/duration.
                context = ('study' if path.startswith('education.') else
                           'professional role' if path in ('experience.recent_roles','experience.job_titles') else
                           'skill mention' if path.startswith('skills.') else 'background mention')
                if re.search(r'\b(?:study|studies|studied|student|course|training)\b',value,re.I):
                    context = 'study'
                elif re.search(r'\b(?:projects?|volunteer)\b',value,re.I):
                    context = 'projects'
                facts.append(dict(_fact(profile,path if '.entries[' in path else f'{path}[{i}]',value),
                                  context=context))
                related.add(option)
                if context == 'professional role':
                    practice.add(option)
    for key in ('hard_constraints','negative_constraints'):
        for i,value in enumerate(profile.get('constraints',{}).get(key,[])):
            if not isinstance(value,str): continue
            negative = re.fullmatch(r'(?:I have )?no (?:hands-on |professional )?experience (?:in|with) (.+?)\.?',value,re.I)
            if negative and negative[1].casefold() in alternatives:
                fact=_fact(profile,f'constraints.{key}[{i}]',value)
                if _explicit(fact):
                    facts.append(fact);denied.add(negative[1].casefold())
    if related & denied:
        return 'unresolved','Your profile gives conflicting background information. Review this condition before relying on it.',facts,[]
    if all(option in denied for option in alternatives):
        return 'contradicted','Your confirmed profile says you do not have the background this condition requests.',facts,[]
    if practice:
        return ('unresolved','Your profile lists a related '+ ' or '.join(sorted(practice))+
                ' role. The requested hands-on work, depth and duration still need confirmation.',facts,
                ['related role: '+x for x in sorted(practice)])
    if related:
        if include_item_experience:
            explanation = _background_item_context(profile, facts)
            if explanation:
                message, context_facts = explanation
                return 'unresolved', message, facts + context_facts, []
            items = list(dict.fromkeys(f['value'] for f in facts if 'context' in f))
            names = ', '.join(items)
            pronoun = 'it' if len(items) == 1 else 'them'
            return ('unresolved', f'Your profile lists {names}, but doesn’t specify whether you’ve used {pronoun} professionally.', facts, [])
        contexts = sorted({f['context'] for f in facts if 'context' in f})
        return ('unresolved','Your profile records related '+ ' / '.join(contexts)+
                ', which does not establish the hands-on professional experience requested here.',facts,[])
    return ('not_established','The requested professional background is not established in your profile. General AI evaluation experience does not establish it.',facts,[])


def _background_item_context(profile, related_facts):
    """Display-only context for labels already recognized by the comparison.

    Never establish a role, proficiency or duration; no new requirement parsing.
    Stronger role evidence and existing contradictions are handled first.
    """
    from wahojobs.profiles.item_experience import FIELDS, PREFIX, linked
    reported = []
    for i, detail in enumerate(profile.get('experience', {}).get('item_details', [])):
        if detail.get('basis') != 'self_reported' or not linked(profile, detail) or not detail['contexts']:
            continue
        root, field = FIELDS[detail['field']]
        if not any(f['value'] == detail['label'] and f['field_path'].startswith(f'{root}.{field}[')
                   for f in related_facts):
            continue  # neither another item nor another collection supplies context
        fact = _fact(profile, f'{PREFIX}[{i}]', detail)
        if not fact['sources'] or not all(s.get('explicit') is True and s.get('source_kind') in
                                         ('user_confirmation', 'user_correction') for s in fact['sources']):
            continue
        reported.append((detail, fact))
    professional = [(d, f) for d, f in reported if 'professional' in d['contexts']]
    if professional:
        names = ', '.join(dict.fromkeys(d['label'] for d, _ in professional))
        return (f'You’ve reported using {names} in your work. Check whether that experience covers the activities described below.',
                [f for _, f in professional])
    if reported:
        # Do not merge different items' contexts into a collective claim.
        phrases = []
        for detail, _ in reported:
            context = ' and '.join({'study': 'studies', 'projects': 'projects'}[c]
                                   for c in ('study', 'projects') if c in detail['contexts'])
            phrases.append(f'{context} involving {detail["label"]}')
        return ('You’ve listed ' + '; '.join(phrases) + '. Experience in a related role isn’t specified.',
                [f for _, f in reported])
    return None


def compare_conditions(packet, profile, *, include_item_experience=False, background_context=None):
    """Only called for visible cards/requested details after identity validation."""
    results = []
    for block in packet['conditions']:
        for line, original in _condition_lines(block):
            quote = re.sub(r'\*\*([^*]+)\*\*', r'\1', original)
            mode = _modality(block['heading'], quote)
            clean = re.sub(r'\s+(?:required|preferred)\.?$', '', quote, flags=re.I)
            result, kind = None, 'unassessed'
            comparators = () if mode in ('not_required', 'unresolved') else (
                ('education', _education), ('tools', _tools), ('workload', _workload),
                ('professional_background', _professional_background))
            for name, compare in comparators:
                # Workload is a stated term, not an inferred qualification.
                result = (compare(clean, profile, include_item_experience=include_item_experience)
                          if name in ('tools', 'professional_background') else compare(clean, profile))
                if result:
                    kind = name; break
            if (kind == 'professional_background' and mode == 'unspecified'
                    and block['heading'].casefold().rstrip(':') in
                    {'qualifications','key qualifications','who you are',"what we're looking for",'what we are looking for'}):
                mode = 'required'
            if result:
                status, message, facts, supported_parts = result
                if mode == 'conflicting':
                    status, message = 'unresolved', 'The source mixes required and preferred wording here. Confirm which applies.'
                if kind == 'workload' and any('different workload terms' in c for c in packet['caveats']):
                    status, message = 'unresolved', 'The listing and description give different workload terms. Confirm the schedule and your availability.'
                if kind == 'workload' and status == 'contradicted' and packet['kind'] == 'Talent network — future consideration':
                    status, message = 'unresolved', 'Your profile says you’re unavailable now. Confirm your availability if a future project is offered.'
                if mode == 'preferred':
                    message = 'Preferred: ' + message[0].lower() + message[1:]
            else:
                status, message, facts, supported_parts = 'unresolved', '', [], []
            if mode == 'not_required':
                kind, status = 'waiver', 'not_applicable'
                message = 'The source explicitly says this is not required.'
            elif mode == 'unresolved':
                message = 'The source mixes a waiver with other wording. Review its scope in the original clause.'
            results.append({'kind': kind, 'modality': mode, 'status': status, 'message': message,
                            'supported_parts': supported_parts, 'profile_facts': facts,
                            'source': {k: packet[k] for k in ('job_id', 'external_id', 'url', 'source_hash', 'captured_at')} |
                                      {'block_reference': block['reference'], 'heading': block['heading'], 'line': line, 'quote': original}})
    # A recognized positive clause cannot override an unparsed qualification or
    # negation about the same named degree/tool elsewhere in the source blocks.
    # Preserve uncertainty instead of attempting to interpret that second clause.
    qualified = [r for r in results if re.search(r'\b(?:not|no|unless|except|only if)\b', r['source']['quote'], re.I)]
    for row in results:
        if row['kind'] not in ('education', 'tools'):
            continue
        pattern = _LEVEL if row['kind'] == 'education' else _TOOLS
        tokens = {t.casefold() for t in re.findall(rf'\b(?:{pattern})\b', row['source']['quote'], re.I)}
        if any(tokens & {t.casefold() for t in re.findall(rf'\b(?:{pattern})\b', q['source']['quote'], re.I)} for q in qualified):
            row.update(status='unresolved', message='The source qualifies or contradicts this condition elsewhere. Review the original wording before relying on it.')
    # Multiple education clauses can be alternatives or genuinely conflicting.
    # Do not resolve that relationship by treating each as cumulative/independent.
    education = [r for r in results if r['kind'] == 'education' and r['modality'] != 'preferred']
    if len({r['source']['quote'] for r in education}) > 1:
        for r in education:
            r.update(status='unresolved', message='The source gives several education conditions. Confirm how they fit together.')
    workloads = [r for r in results if r['kind'] == 'workload']
    if len({r['source']['quote'] for r in workloads}) > 1:
        for r in workloads:
            r.update(status='unresolved', message='The source gives several workload terms. Confirm which schedule applies and your availability.')
    _background_components(results, packet, profile, background_context)
    _professional_alternative_groups(results, packet, profile)
    for row in results:
        if 'components' in row:
            row['components']['other_qualifications'] = [
                dict(kind=r['kind'], modality=r['modality'], status=r['status'], source=deepcopy(r['source']))
                for r in results if r is not row and not (
                    row.get('requirement_group') and row.get('requirement_group') == r.get('requirement_group'))]
    return results


def _condition_lines(block):
    """Keep clear independent waiver clauses and existing alternative routes.

    Quotes remain exact substrings of the original line and retain its source
    block/line reference. Exceptions and ambiguous relations are not split.
    """
    from wahojobs.professional_background_duration import requirement
    for line, original in _lines(block):
        waiver_parts = re.split(r';\s+|(?<=\.)\s+|,\s+but\s+', original, maxsplit=1, flags=re.I)
        if (len(waiver_parts) == 2 and _waiver_modality(waiver_parts[0]) == 'not_required'
                and not re.match(r'(?:unless|except|if|only if)\b', waiver_parts[1], re.I)):
            yield line, waiver_parts[0]
            yield line, waiver_parts[1]
            continue
        parts = re.split(r'(?<=\.)\s+(?=Alternatively,|The experience requirement)', original, maxsplit=1, flags=re.I)
        if len(parts) == 2 and requirement(parts[0]):
            yield line, parts[0]
            yield line, parts[1]
        else:
            yield line, original


def _professional_alternative_groups(rows, packet, profile):
    """Bounded adjacent degree route / same-block experience waiver contract.

    Each member keeps its own comparison, while status is the grouped result
    consumed by existing qualification review. No component is a separate veto.
    Unrelated alternatives, different blocks and preferred credentials do not
    waive years. Unsupported local degree-route wording adds uncertainty only.
    """
    from wahojobs.professional_background_duration import requirement
    from wahojobs.professional_background_semantics import digest
    groups = {}
    for i, alternate in enumerate(rows):
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', alternate['source']['quote']).strip()
        adjacent = bool(re.match(r'Alternatively,\s+', text, re.I) and
                        re.search(r'\bdegree\b|\b(?:is|may be) sufficient\.?$', text, re.I))
        waiver = bool(re.match(r'The experience requirement is optional for applicants with\b', text, re.I))
        if (not (adjacent or waiver) or alternate['modality'] in ('preferred', 'conflicting')
                or re.search(r'\bpreferred\b|\bnot sufficient\b', text, re.I)):
            continue
        previous = [j for j in range(i) if rows[j]['source']['block_reference'] == alternate['source']['block_reference']
                    and rows[j]['kind'] == 'professional_background' and requirement(rows[j]['source']['quote'])
                    and rows[j]['modality'] not in ('preferred', 'conflicting')]
        if not previous:
            continue
        if adjacent:
            # "Alternatively" refers to the immediately preceding requirement,
            # not any experience mention elsewhere in the document.
            targets = [j for j in previous if j == i - 1 or i - 1 in groups.get(j, {}).get('members', [])]
        else:
            targets = previous  # generic "the experience requirement" may be ambiguous
        if not targets:
            continue
        parsed = (re.fullmatch(r'Alternatively, a degree in (?P<field>[\w -]{1,128}) is sufficient\.?', text, re.I)
                  if adjacent else re.fullmatch(r'The experience requirement is optional for applicants with a (?P<field>[\w -]{1,128}) degree\.?', text, re.I))
        field = parsed['field'] if parsed else None
        if field and re.search(r'\b(?:and|or|not|unless|if|only)\b', field, re.I):
            field = None
        compared = _education('degree in ' + field, profile, generic_degree=True) if field else None
        for j in targets:
            group = groups.setdefault(j, dict(members=[], routes=[], certain=True))
            # A generic waiver cannot establish which requirement it replaces
            # across intervening, potentially unparsed qualification wording.
            group['certain'] &= len(targets) == 1 and field is not None and (not waiver or j == i - 1)
            group['members'].append(i)
            status, message, facts, parts = compared or ('unresolved', 'The degree route needs clarification.', [], [])
            group['routes'].append(dict(kind='degree', status=status, message=message,
                                       profile_facts=facts, supported_parts=parts, source=deepcopy(alternate['source'])))
    for j, group in groups.items():
        row = rows[j]
        professional = dict(kind='professional_background', status=row['status'], message=row['message'],
                            source=deepcopy(row['source']), profile_facts=deepcopy(row['profile_facts']))
        routes = [professional] + group['routes']
        statuses = [r['status'] for r in routes]
        status = ('supported' if 'supported' in statuses else
                  'contradicted' if all(s == 'contradicted' for s in statuses) else 'unresolved') if group['certain'] else 'unresolved'
        context = dict(operator='any_of' if group['certain'] else 'unresolved', status=status, routes=routes,
                       source_text_digest=digest(packet['text']))
        group_id = digest(dict(source_context=context['source_text_digest'], sources=[r['source'] for r in routes]))
        row['components']['qualifying_routes'] = context
        messages = {
            'supported': 'Your confirmed degree supports this alternative requirement. Other role qualifications remain separate.',
            'contradicted': 'Every qualifying route in this requirement conflicts with your confirmed profile.',
            'unresolved': 'The experience route and the degree alternative must be considered together; this requirement remains unresolved.',
        }
        for k, route in zip([j] + group['members'], routes):
            member = rows[k]
            member['route_comparison'] = deepcopy(route)
            member.update(status=status, requirement_group=group_id, message=messages[status])


def _background_components(rows, packet, profile, context):
    """Keep positive relevance separate from duration, depth and other criteria.

    Semantic output can add only occupational relevance. It cannot clear an
    objective contradiction, establish the whole requirement or waive another
    clause. Invalid/missing output leaves independently valid comparisons alone.
    """
    from wahojobs.professional_background_duration import VERSION, compare_duration
    from wahojobs.professional_background_semantics import ComparisonContext, build_request
    for row in rows:
        if row['kind'] != 'professional_background':
            continue
        duration = compare_duration(row['source']['quote'], profile)
        relevance = dict(status='supported_partial' if row['supported_parts'] else 'not_established',
                         basis='existing_professional_comparison', supported_parts=list(row['supported_parts']))
        if type(context) is ComparisonContext:
            request = build_request(packet, row, profile, context)
            semantic = context.evidence.lookup(request)
            if semantic is not None:
                relevance['semantic'] = dict(semantic, basis=context.evidence.basis,
                                             recipe=context.evidence.recipe, model=context.evidence.model)
                if semantic['relation'] == 'supported_partial':
                    part = 'related occupational area (prepared semantic evidence; not verified competence)'
                    row['supported_parts'] = list(dict.fromkeys(row['supported_parts'] + [part]))
                    relevance.update(status='supported_partial', supported_parts=list(row['supported_parts']))
                    row['profile_facts'] += [deepcopy(request['candidate_facts'][key]) for key in semantic['candidate_fact_ids']]
                    if row['status'] != 'contradicted':
                        row['status'] = 'unresolved'
                        row['message'] = ('Your declared role has partial occupational relevance. '
                            'The complete professional requirement is not established. ' + row['message'])
                elif not row['supported_parts']:
                    relevance['status'] = semantic['relation']
        row['components'] = dict(version=VERSION, occupational_relevance=relevance, required_duration=duration,
            responsibilities_and_depth=dict(status='unresolved', source=deepcopy(row['source'])))


def render_comparisons(packet, *, block_reference=None, highlights=False):
    rows = [r for r in packet.get('comparisons', []) if r['message']]
    if block_reference is not None:
        rows = [r for r in rows if r['source']['block_reference'] == block_reference]
    if highlights:
        education = next((r for r in rows if r['kind'] == 'education'), None)
        question = next((r for r in rows if r['kind'] != 'education' and r['status'] == 'contradicted'), None)
        question = question or next((r for r in rows if r['kind'] == 'workload'), None)
        rows = [r for r in (education, question) if r]
    # Only strip a modality prefix when this very block visibly replaces it.
    # Mixed/unassessed blocks and highlights keep their existing presentation.
    block_rows = [r for r in packet.get('comparisons', [])
                  if r['source']['block_reference'] == block_reference]
    modes = {r['modality'] for r in block_rows}
    if (not highlights and block_reference is not None and rows and
            all(r['kind'] == 'professional_background' for r in block_rows) and
            len(modes) == 1 and modes <= {'required', 'preferred'}):
        mode = next(iter(modes))
        messages = []
        for row in rows:
            message = row['message']
            if mode == 'preferred' and message.startswith('Preferred: '):
                message = message[len('Preferred: '):]
                message = message[0].upper() + message[1:]
            messages.append('<p>' + escape(message) + '</p>')
        return ("<div class='candidate-comparisons'><strong>Your background</strong>" +
                ''.join(messages) + '</div><p><strong>Employer ' +
                ('preference' if mode == 'preferred' else 'requirement') + '</strong></p>')
    return ("<ul class='candidate-comparisons candidate-caveats'>" +
            ''.join('<li>' + escape(r['message']) + '</li>' for r in rows) + '</ul>') if rows else ''

"""Bounded, non-exclusionary comparisons for an already-selected source packet.

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
             'required skills and qualifications'}
_PREFERRED = {'preferred', 'preferred qualifications', 'ideal qualifications', 'nice to have'}


def _fact(profile, path, value):
    return {'field_path': path, 'value': deepcopy(value), 'sources': deepcopy([
        ref for ref in profile.get('provenance', {}).get('field_sources', [])
        if ref.get('field_path') == path or ref.get('field_path', '').startswith((path + '[', path + '.'))])}


def _explicit(fact):
    return any(ref.get('explicit') is True for ref in fact['sources'])


def _modality(heading, quote):
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
        elif re.match(r'^\s*[-*+]\s+', line) or not current:
            if current:
                yield start, current
            current, start = re.sub(r'^\s*[-*+]\s+', '', line).strip(), number
        else:
            current += ' ' + line.strip()
    if current:
        yield start, current


def _education(quote, profile):
    match = re.fullmatch(rf'({_LEVEL})(?: or ({_LEVEL}))? in (.+?)\.?', quote, re.I)
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
    levels = [level(s) for s in match.groups()[:2] if s]
    facts, matching, degree_only = [], [], []
    for i, entry in enumerate(profile.get('education', {}).get('entries', [])):
        facts.extend(_fact(profile, f'education.entries[{i}].{key}', entry.get(key))
                     for key in ('kind', 'qualification', 'field', 'status'))
        if (entry.get('kind'), entry.get('status')) in levels:
            degree_only.append(entry)
            if normalize_comparison_label(entry.get('field', '')) in map(normalize_comparison_label, exact_fields):
                matching.append(entry)
    level_fact = _fact(profile, 'education.education_level', profile.get('education', {}).get('education_level'))
    facts.append(level_fact)
    if level_fact['value'] == 'no_degree' and _explicit(level_fact):
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


def _professional_background(quote, profile):
    """Compare a few explicit background forms, without a profession taxonomy.

    The alternatives come from the source, not the title. Study/skill mentions
    remain distinct from roles/practice and never prove hands-on work or years.
    Generic annotation of a domain does not establish that domain's profession.
    """
    text = quote.strip().rstrip('.')
    if re.search(r'\b(?:not|no|unless|except|if|preferred|ideal|optional)\b', text, re.I):
        return None
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
        contexts = sorted({f['context'] for f in facts if 'context' in f})
        return ('unresolved','Your profile records related '+ ' / '.join(contexts)+
                ', which does not establish the hands-on professional experience requested here.',facts,[])
    return ('not_established','The requested professional background is not established in your profile. General AI evaluation experience does not establish it.',facts,[])


def compare_conditions(packet, profile, *, include_item_experience=False):
    """Only called for visible cards/requested details after identity validation."""
    results = []
    for block in packet['conditions']:
        for line, original in _lines(block):
            quote = re.sub(r'\*\*([^*]+)\*\*', r'\1', original)
            mode = _modality(block['heading'], quote)
            clean = re.sub(r'\s+(?:required|preferred)\.?$', '', quote, flags=re.I)
            result, kind = None, 'unassessed'
            for name, compare in (('education', _education), ('tools', _tools), ('workload', _workload),
                                  ('professional_background', _professional_background)):
                # Workload is a stated term, not an inferred qualification.
                result = (compare(clean, profile, include_item_experience=include_item_experience)
                          if name == 'tools' else compare(clean, profile))
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
    return results


def render_comparisons(packet, *, block_reference=None, highlights=False):
    rows = [r for r in packet.get('comparisons', []) if r['message']]
    if block_reference is not None:
        rows = [r for r in rows if r['source']['block_reference'] == block_reference]
    if highlights:
        education = next((r for r in rows if r['kind'] == 'education'), None)
        question = next((r for r in rows if r['kind'] != 'education' and r['status'] == 'contradicted'), None)
        question = question or next((r for r in rows if r['kind'] == 'workload'), None)
        rows = [r for r in (education, question) if r]
    return ("<ul class='candidate-comparisons candidate-caveats'>" +
            ''.join('<li>' + escape(r['message']) + '</li>' for r in rows) + '</ul>') if rows else ''

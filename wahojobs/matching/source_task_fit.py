"""Bounded source-task evidence supplement to affirmative fit.

Language support is distinct from substantive work support. This recognizes
explicit linguistic-analysis/instruction tasks, not a general qualification
parser. It never scores, interprets degrees as mandatory, or excludes a job.
"""
from copy import deepcopy
import re

from wahojobs.authenticated_card_evidence import _source_text
from wahojobs.matching.fit_evidence import SUPPORTED, UNCERTAIN
from wahojobs.profiles.normalizer import term_is_negated


_SPECIALTIES = r'\b(?:phonetics|morphology|syntax|semantics|pragmatics|sociolinguistics)\b'
_ANALYSIS_TASK = re.compile(
    r'\b(?:analy[sz]e|evaluat\w*|assess|challenge|test|annotat\w*)\b.{0,160}'
    r'\b(?:language models?|linguistic|phoneti\w*|morpholog\w*|syntax|semantics|pragmatics)\b'
    r'|\b(?:perform|conduct|undertake)\s+(?:specialist\s+)?linguistic analysis\b', re.I)
_INSTRUCTION_TASK = re.compile(
    r"\b(?:you will|you.ll|responsibilities include)\s+(?:be\s+)?(?:teach|teaching|instruct|instructing)\b"
    r'.{0,100}\b(?:language|students?|classes|lessons)\b', re.I)
_APPLICANT_TASK = re.compile(
    r"\b(?:you(?:['’]ll| will)|your (?:work|tasks|responsibilities)|"
    r"(?:this|the) role involves|responsibilities|scope of work)\b", re.I)
_BEGINNER = re.compile(r'\bno\s+(?:prior\s+|previous\s+|professional\s+)?experience\s+(?:is\s+)?(?:needed|required)\b', re.I)
_OPTIONAL = re.compile(r'\b(?:preferred|ideal|nice to have|optional|not required)\b', re.I)
_RELATED_ANALYSIS = r'\b(?:translation|translator|translated|proofreading|proofreader|linguist(?:ics|ic analysis)?|language teaching|language teacher|editor|editing)\b'
_RELATED_INSTRUCTION = r'\b(?:language teach(?:ing|er)|taught|teaching|teacher|instructor)\b'


def _task(text):
    # Accepted paragraph boundaries keep preferred credentials separate from
    # task descriptions. Unsupported mixed wording is left unassessed.
    # Generic promises to train beginners are not specialist prerequisites.
    if _BEGINNER.search(text):
        return None
    for sentence in re.split(r'\n\s*\n', text):
        if _OPTIONAL.search(sentence):
            continue
        marker = _ANALYSIS_TASK.search(sentence)
        specialties = set(re.findall(_SPECIALTIES, sentence, re.I))
        if (marker and _APPLICANT_TASK.search(sentence[:marker.start()])
                and (len(specialties) >= 2 or 'linguistic analysis' in marker.group().lower())):
            kind, label = 'linguistic_analysis', 'Linguistic analysis tasks'
        elif (marker := _INSTRUCTION_TASK.search(sentence)):
            kind, label = 'language_instruction', 'Language teaching tasks'
        else:
            continue
        if (term_is_negated(sentence.lower(), marker.start(), marker.end())
                or re.search(r'\b(?:not|never|no need to)\s*$', sentence[:marker.start()], re.I)):
            continue
        return dict(kind=kind, label=label, quote=sentence.strip())
    return None


def _related_facts(profile, kind):
    pattern = re.compile(_RELATED_INSTRUCTION if kind == 'language_instruction' else _RELATED_ANALYSIS, re.I)
    paths = [('experience.recent_roles', profile.get('experience', {}).get('recent_roles', [])),
             ('skills.normalized', profile.get('skills', {}).get('normalized', [])),
             ('skills.free_text_labels', profile.get('skills', {}).get('free_text_labels', []))]
    facts = []
    for path, values in paths:
        for index, value in enumerate(values):
            if not isinstance(value, str):
                continue
            if re.search(r'\b(?:want to|interested in|hope to|learning to)\b', value, re.I):
                continue
            for term in pattern.finditer(value):
                if term_is_negated(value.lower(), term.start(), term.end()):
                    continue
                # Editing is only related here when declared as a language skill
                # or when the role supplies text/language context, not video work.
                if term.group().lower() in {'editing', 'editor'} and re.search(r'\b(?:video|audio|photo|image)\b', value, re.I):
                    continue
                facts.append({'path': f'{path}[{index}]', 'text': value})
                break
    return facts


def apply_source_task_fit(match, source, profile):
    from wahojobs.matching.accepted_tasks import apply_task_condition_review
    match = _apply_language_task_fit(match, source, profile)
    return apply_task_condition_review(match, source, profile)


def _apply_language_task_fit(match, source, profile):
    """Supplement the existing assessment for this exact accepted source only.

    Call before typed admission, on the bounded pre-admission pool or a scoped
    detail group. No source identity inference, persistent state or new score.
    """
    if not match.get('matched_languages') or not source:
        return match
    if any(source.get(k) != match.get(k) for k in ('job_id', 'canonical_opportunity_id', 'url', 'source_slug')):
        return match
    if (source.get('content_external_id') != source.get('external_id')
            or source.get('source_url') != source.get('url')):
        return match
    try:
        task = _task(_source_text(source))
    except (ValueError, TypeError, KeyError):
        return match
    if task is None:
        return match
    facts = _related_facts(profile, task['kind'])
    reference = dict(job_id=source['job_id'], external_id=source['external_id'],
                     source_url=source['source_url'], source_slug=source['source_slug'],
                     material_content_sha256=source.get('material_content_sha256'),
                     captured_at=source.get('last_captured_at'))
    result = dict(task, source_reference=reference, profile_facts=facts,
                  status=SUPPORTED if facts else UNCERTAIN)
    if match.get('conditional_task_fit') and match.get('affirmative_fit_status') == UNCERTAIN:
        result['status'] = UNCERTAIN  # related work cannot resolve a separate language-level question
    updated = dict(match, source_task_fit=result)
    assessment = deepcopy(match['affirmative_fit'])
    assessment['supported_evidence'] = [e for e in assessment['supported_evidence']
                                       if e['requirement'] not in ('General language-data work', 'Bilingual work')]
    assessment['satisfied_groups'] = [s for s in assessment['satisfied_groups']
                                      if s not in ('General language-data work', 'Bilingual work')]
    assessment['required_groups'] = list(assessment['required_groups']) + [
        dict(key='source_task:' + task['kind'], label=task['label'], mode='any_of',
             concepts=(task['kind'],), source='accepted_source_task')]
    if facts:
        assessment['supported_evidence'].append(dict(
            requirement='Related language-task background', profile_evidence=facts[0]['text'], source=facts[0]['path']))
        # Related work is enough for a credible possibility, not proof of every
        # specialty, degree, proficiency level or application condition.
        note = ('Your language-work background is relevant. Check the specialist tasks before applying.'
                if task['kind'] == 'linguistic_analysis' else
                'Your teaching background is relevant. Check the teaching duties before applying.')
    else:
        assessment['missing_requirements'] = list(assessment['missing_requirements']) + [task['label'] + ': related background not established']
        if assessment['status'] == SUPPORTED:
            assessment['status'] = UNCERTAIN
            updated['conditional_task_fit'] = True
        updated['primary_recommendation_eligible'] = False
        updated['primary_admission_source'] = 'affirmative_fit_' + assessment['status']
        updated['primary_admission_reasons'] = list(dict.fromkeys(
            list(match.get('primary_admission_reasons') or []) + ['affirmative_fit_' + assessment['status']]))
        note = ('Language fluency matches; linguistic analysis experience is not stated. Consider this if you have that background.'
                if task['kind'] == 'linguistic_analysis' else
                'Language fluency matches; teaching experience is not stated. Consider this if you have that background.')
    result['candidate_note'] = note
    assessment['why_fit_statements'] = [note]
    updated.update(affirmative_fit=assessment, affirmative_fit_status=assessment['status'],
                   affirmative_fit_supported_evidence=assessment['supported_evidence'], affirmative_fit_why=[note])
    return updated


def is_conditional_task_fit(match):
    return (match.get('conditional_task_fit') is True
            and match.get('affirmative_fit_status') == UNCERTAIN
            and (match.get('source_task_fit') or {}).get('status') == UNCERTAIN)

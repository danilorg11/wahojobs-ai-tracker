"""Project explicit accepted duties into the existing AI-evaluation signal.

This is not a qualification parser. It retains quotations and exact variant
identity; it never infers degrees, proficiency, availability or permission.
Prepared facts are reused for unchanged accepted material, including by scoped
details. No descriptions are fetched or persisted by this projection.
"""
from functools import lru_cache
import json
import re

from wahojobs.authenticated_card_evidence import _source_text, load_card_sources
from wahojobs.profiles.normalizer import term_is_negated


TASK_PROJECTION_VERSION = 6
SOURCE_ELIGIBILITY_VERSION = 5
TASK_ADMISSION_VERSION = 12
_DUTY_HEADING = re.compile(
    r"^(?:key |main |core )?(?:responsibilities|duties|scope of work|job details|"
    r"role overview|what the work looks like|what you(?:'ll| will) (?:do|work on)|your (?:work|tasks|responsibilities))$", re.I)
_OTHER_HEADING = re.compile(
    r"^(?:(?:minimum|required|preferred|ideal|additional|key) )?(?:qualifications|requirements)$|"
    r"^education & experience$|"
    r"^(?:about(?: .+)?|benefits|compensation(?: structure)?|(?:project )?timeline|"
    r"onboarding|application(?: & onboarding| screening questions)?|more details|perks|"
    r"why join(?: .+)?|who you are|what we(?:'re| are) looking for|nice to have|"
    r"project details|start timeline & availability|other published fields(?: .+)?)$", re.I)
_APPLICANT = re.compile(
    r"\b(?:you(?:['’]ll| will)|your (?:tasks|work|responsibilities)|"
    r"(?:the )?(?:successful )?candidate will)\b", re.I)
# Match a task's action and object separately, rather than requiring one exact
# phrase such as "model outputs". This is still a bounded task vocabulary.
_EVALUATE = r"evaluat\w*|review\w*|assess\w*|compar\w*|rat(?:e|ing)|grad(?:e|es|ed|ing)|scor\w*|validat\w*|check\w*"
_ANNOTATE = r"annotat\w*|label\w*|tag(?:s|ging)?|curat\w*"
_AUTHOR = r"writ\w*|creat\w*|draft\w*|design\w*|develop\w*|refin\w*|improv\w*"
_EVALUATION_OBJECT = re.compile(
    r"\b(?:responses?|outputs?|answers?|conversations?|rubrics?|"
    r"videos?|images?|audio|footage|clips|data ?sets?)\b", re.I)
_DATA_OBJECT = re.compile(
    r"\b(?:data(?: ?sets?)?|videos?|images?|audio|text|events?|responses?|outputs?)\b", re.I)
_AUTHOR_OBJECT = re.compile(r"\b(?:prompts?|(?:scoring |evaluation )?rubrics?)\b", re.I)
_AI_CONTEXT = re.compile(r"\b(?:AI|LLMs?|language models?|model outputs?|AI training)\b", re.I)
_NON_DUTY = re.compile(
    r"^(?:our\b|we\b|they\b|(?:this|the) (?:company|platform|product|model|system|project)\b)|"
    r"\b(?:interested in|passion for|aspire to|hope to|experience (?:in|with|of)|"
    r"(?:must|should|will) have (?:experience|expertise))\b", re.I)
_OPTIONAL = re.compile(r"\b(?:preferred|ideal|nice to have|optional|not required)\b", re.I)


def _source_paragraphs(source):
    """Reuse accepted paragraph boundaries, including captured ATS list blocks."""
    text = _source_text(source, include_structured_lists=False)
    if re.match(r'\s*<(?:p|div|h[1-6]|ul|section)\b', text, re.I):
        from wahojobs.opportunity_enrichment import source_body_paragraphs
        text = '\n\n'.join(source_body_paragraphs(text, 'text/html'))
    for i, paragraph in enumerate(re.split(r"\n\s*\n", text)):
        yield f'accepted text paragraph {i + 1}', paragraph
    metadata = json.loads(source.get('metadata_json') or '{}')
    # These are already accepted source blocks, not inferred requirements or a
    # second fetch. Never read unrelated metadata strings as applicant duties.
    lists = metadata.get('lists', [])
    if isinstance(lists, list):
        from wahojobs.opportunity_enrichment import source_body_paragraphs
        for i, block in enumerate(lists):
            if not isinstance(block, dict) or not all(isinstance(block.get(k), str) for k in ('text', 'content')):
                continue
            yield f'metadata.lists[{i}].text', block['text']
            for j, paragraph in enumerate(source_body_paragraphs(block['content'], 'text/html')):
                yield f'metadata.lists[{i}].content paragraph {j + 1}', paragraph


def _task_action(clause, *, ai_context):
    """Require an assigned action/object pair; qualifications stay elsewhere."""
    pairs = (
        (_EVALUATE, _EVALUATION_OBJECT),
        (_EVALUATE, re.compile(r'\bcontent\b', re.I)),
        (_ANNOTATE, _DATA_OBJECT),
        (_AUTHOR, _AUTHOR_OBJECT),
    )
    for verbs, objects in pairs:
        for action in re.finditer(r'\b(?:' + verbs + r')\b', clause, re.I):
            obj = objects.search(clause)
            if not obj or abs(obj.start() - action.start()) > 180:
                continue
            if action.group().casefold().startswith('grad'):
                # Classroom grading is not AI evaluation merely because a
                # company paragraph elsewhere mentions AI. This newly supported
                # action needs its own explicit model-output object.
                # Keep the demonstrated action directly bound to its object;
                # a contrast such as "grade essays, not AI outputs" is no duty.
                obj = re.match(r'\s+(?:the\s+)?(?:model|AI(?:[- ](?:generated|produced))?|LLM|language model)\s+'
                               r'(?:responses?|outputs?|answers?)\b', clause[action.end():], re.I)
                if not obj:
                    continue
            # Content review also describes ordinary editorial work. The new
            # object needs AI linkage in the duty itself, not an optional tool
            # or company paragraph elsewhere in the accepted source.
            if obj.group().casefold() == 'content':
                ai_content = re.search(
                    r'\b(?:AI|LLM|language model)[ -](?:generated|produced)\s+content\b|'
                    r'\bcontent\s+(?:generated|produced)\s+by\s+(?:AI|LLMs?|(?:a |the )?language models?)\b',
                    clause, re.I)
                if (not ai_content or term_is_negated(clause.lower(), ai_content.start(), ai_content.end())
                        or re.search(r'\b(?:not|non)[ -]*$', clause[:ai_content.start()], re.I)):
                    continue
            # A bare answer, prompt, or rubric outside AI work is not evidence
            # of professional AI evaluation (e.g. a classroom teaching duty).
            if not ai_context and not re.search(r'\b(?:video|image|audio|footage|clips|data|datasets|data sets)\b', clause, re.I):
                continue
            prefix = clause[:action.start()]
            if (term_is_negated(clause.lower(), action.start(), action.end())
                    or re.search(r"\b(?:not(?!\s+only\b)|never|no need to|don['’]t)\b[^.;]{0,70}$", prefix, re.I)):
                continue
            return action
    return None


@lru_cache(maxsize=16384)
def _prepare(material_hash, provider, external_id, url, body, body_format, metadata_json):
    # Including bytes as well as their recorded hash prevents reuse of a stale
    # hash after an incorrectly updated accepted row. No cross-variant identity
    # or applicant restrictions are taken from a cached result.
    source = dict(source_slug=provider, external_id=external_id, url=url,
                  body=body, body_format=body_format, metadata_json=metadata_json)
    try:
        paragraphs = tuple(_source_paragraphs(source))
    except (ValueError, TypeError, KeyError):
        return ()
    ai_context = any(_AI_CONTEXT.search(text) for _, text in paragraphs)
    duties = False
    excluded_block = False
    facts = []
    for reference, paragraph in paragraphs:
        quote = paragraph.strip()
        heading = quote.strip("#*: \n").strip().replace('’', "'")
        if _DUTY_HEADING.fullmatch(heading):
            duties = True
            excluded_block = False
            continue
        if _OTHER_HEADING.fullmatch(heading):
            duties = False
            excluded_block = True
            continue
        if not quote or excluded_block or _OPTIONAL.search(quote):
            continue
        # Only applicant duties, never company boilerplate or an aspirational
        # overview saying that a company trains models. Each fact is a clause,
        # not a bag of words combined across qualification/marketing blocks.
        if not duties and not _APPLICANT.search(quote):
            continue
        for clause in re.split(r"(?<=[.!?;])\s+|\n(?=\s*[-*])", quote):
            clean = re.sub(r'[*#]', '', clause).lstrip(' -\t')
            if _NON_DUTY.search(clean):
                continue
            action = _task_action(clause, ai_context=ai_context)
            if not action:
                continue
            if re.search(r"\b(?:partners|clients|researchers|our team|our company)\b", clause[:action.start()], re.I):
                continue
            from scripts.profile_match_digest import detect_role_match_features
            domains = tuple(sorted(detect_role_match_features(clause)['professional_domains']))
            facts.append((clause.strip(), domains, reference))
    return tuple(dict.fromkeys(facts))


def project_accepted_tasks(connection, rows):
    """Read the accepted-content view for exactly the queried variants.

    First use prepares each distinct accepted body once. Subsequent matching
    and detail reads reuse the versioned projection, recomputing only changed
    material (or entries evicted from the bounded cache).
    """
    sources = load_card_sources(connection, rows)
    result = []
    for row in rows:
        source = sources.get(row['job_id'])
        valid = source and all(source.get(k) == row.get(k) for k in
                               ('job_id', 'canonical_opportunity_id', 'url', 'source_slug'))
        valid = (valid and source.get('content_provider') == source.get('source_slug')
                 and source.get('content_external_id') == source.get('external_id')
                 and source.get('source_url') == source.get('url'))
        if not valid:
            result.append(row)
            continue
        language_conditions, country_conditions = _prepare_eligibility(
            source.get('material_content_sha256'), source['source_slug'],
            source['external_id'], source['url'], source.get('body'),
            source.get('body_format'), source.get('metadata_json'))
        reference = {k: source.get(k) for k in ('job_id', 'canonical_opportunity_id',
                     'external_id', 'source_slug', 'source_url', 'material_content_sha256', 'last_captured_at')}
        if language_conditions or country_conditions:
            row = dict(row, accepted_eligibility_evidence=dict(
                version=SOURCE_ELIGIBILITY_VERSION, source_reference=reference,
                language_conditions=language_conditions, country_conditions=country_conditions))
        facts = _prepare(source.get('material_content_sha256'), source['source_slug'],
                         source['external_id'], source['url'], source.get('body'),
                         source.get('body_format'), source.get('metadata_json'))
        if not facts:
            result.append(row)
            continue
        reference = {k: source.get(k) for k in ('job_id', 'canonical_opportunity_id',
                     'external_id', 'source_slug', 'source_url', 'material_content_sha256', 'last_captured_at')}
        evidence = dict(version=TASK_PROJECTION_VERSION, source_reference=reference,
                        facts=[dict(quote=q, professional_domains=list(d), block_reference=b)
                               for q, d, b in facts])
        evidence['transferable_scope'] = list(_prepare_transferable_scope(
            source.get('material_content_sha256'), source['source_slug'],
            source['external_id'], source['url'], source.get('body'),
            source.get('body_format'), source.get('metadata_json')))
        evidence['beginner_scope'] = list(_prepare_beginner_scope(
            source.get('material_content_sha256'), source['source_slug'],
            source['external_id'], source['url'], source.get('body'),
            source.get('body_format'), source.get('metadata_json')))
        result.append(dict(row, accepted_task_evidence=evidence))
    return result


@lru_cache(maxsize=16384)
def _prepare_beginner_scope(material_hash, provider, external_id, url, body, body_format, metadata_json):
    from wahojobs.matching.beginner_access import source_scope
    try:
        return tuple(source_scope(dict(source_slug=provider, external_id=external_id,
            url=url, body=body, body_format=body_format, metadata_json=metadata_json)))
    except (ValueError, TypeError, KeyError):
        return ()


@lru_cache(maxsize=16384)
def _prepare_transferable_scope(material_hash, provider, external_id, url, body, body_format, metadata_json):
    from wahojobs.matching.transferable_tasks import source_scope
    try:
        return tuple(source_scope(dict(source_slug=provider, external_id=external_id,
            url=url, body=body, body_format=body_format, metadata_json=metadata_json)))
    except (ValueError, TypeError, KeyError):
        return ()


@lru_cache(maxsize=16384)
def _prepare_eligibility(material_hash, provider, external_id, url, body, body_format, metadata_json, *, include_ungraded_languages=False):
    """Reuse accepted source blocks/cache before per-variant comparisons.

    No request-time fetch and no repeated description parsing for unchanged
    evidence. Main/conditional/fallback and scoped details share these facts.
    """
    from wahojobs.authenticated_card_evidence import _blocks, _QUALIFICATION_HEADINGS
    from wahojobs.candidate_condition_comparisons import _condition_lines, _modality
    from wahojobs.matching.languages import prepare_language_conditions
    from wahojobs.matching.source_geography import prepare_applicant_residence_clause
    source = dict(body=body, body_format=body_format, metadata_json=metadata_json,
                  source_slug=provider, external_id=external_id, url=url)
    try:
        blocks = _blocks(_source_text(source))
    except (ValueError, TypeError, KeyError):
        return (), ()
    languages, countries = [], []
    applicant_headings = {'who you are', "what we're looking for", 'what we are looking for', 'what we’re looking for'}
    for block in blocks:
        heading = block['heading'].casefold().rstrip(':')
        if heading not in _QUALIFICATION_HEADINGS:
            continue
        for line, quote in _condition_lines(block):
            mode = _modality(heading, quote)
            if mode == 'unspecified' and heading in applicant_headings:
                mode = 'required'  # explicit applicant criteria, not marketing
            if mode == 'conflicting':
                mode = 'unresolved'
            ref = f"{block['reference']}:line {line}"
            language_conditions = prepare_language_conditions(
                quote, mode, include_ungraded=include_ungraded_languages)
            if mode == 'not_required':
                language_conditions = [dict(c, modality=mode) for c in language_conditions]
            languages.extend(dict(c, source_field=ref, heading=block['heading'])
                             for c in language_conditions)
            place = prepare_applicant_residence_clause(quote, mode, ref)
            if place:
                countries.append(place)
    for condition in languages:
        if condition['modality'] == 'required' and any(
                other['modality'] == 'not_required' and set(condition['languages']) & set(other['languages'])
                for other in languages):
            condition['modality'] = 'unresolved'
    # These invitations were prepared during identity-validated detail
    # ingestion/reprocessing. No new description grammar runs in requests.
    from hashlib import sha256
    from wahojobs.source_capture import normalize_source_body
    metadata = json.loads(metadata_json or '{}')
    detail = metadata.get('wahojobs_source_detail_v1') or {}
    prepared = detail.get('applicant_location_support') or {}
    if (detail.get('provider') == provider and detail.get('external_id') == external_id
            and detail.get('url') == url and prepared.get('version') == 1
            and prepared.get('body_sha256') == sha256((normalize_source_body(body) or '').encode()).hexdigest()):
        countries.extend(prepared['clauses'])
    return tuple(languages), tuple(countries)


def apply_accepted_eligibility(profile, row, match):
    """Apply shared language/country contracts before canonical selection.

    Explicit conflicts veto every recommendation route. Unknown levels may
    retain existing task-supported conditional admission, never new task fit.
    This changes no numeric score or threshold.
    """
    from copy import deepcopy
    from dataclasses import asdict
    from wahojobs.matching.languages import compare_language_condition
    from wahojobs.matching.locations import location_eligibility
    evidence = row.get('accepted_eligibility_evidence') or {}
    ref = evidence.get('source_reference') or {}
    if (evidence.get('version') != SOURCE_ELIGIBILITY_VERSION
            or any(ref.get(k) != row.get(k) for k in ('job_id', 'canonical_opportunity_id', 'source_slug'))
            or ref.get('source_url') != row.get('url')):
        return match
    checks = [dict(compare_language_condition(profile, c), source_reference=ref)
              for c in evidence['language_conditions']]
    location_check = None
    if evidence['country_conditions']:
        location_check = location_eligibility(profile, dict(row, applicant_country_requirements=
            list(row.get('applicant_country_requirements') or []) + list(evidence['country_conditions'])))
    conflicts = [c for c in checks if c['modality'] == 'required' and c['status'] == 'contradicted']
    unknowns = [c for c in checks if c['modality'] in ('required', 'unresolved')
                and c['status'] in ('not_established', 'unresolved')]
    location_conflict = location_check and location_check.status == 'incompatible'
    match = dict(match, source_language_checks=checks, accepted_eligibility_evidence=evidence)
    if location_check:
        match['source_task_location_checks'] = [dict(
            status=checked.status, reason=checked.reason, quote=c['source_quote'],
            source_reference=c['source_field'], job_id=row['job_id'],
            source_hash=ref.get('material_content_sha256'))
            for c in evidence['country_conditions']
            for checked in [location_eligibility(profile, dict(row, applicant_country_requirements=[c]))]]
    if location_check and match.get('location_eligibility_status') != 'incompatible':
        match.update(source_applicant_location_check=asdict(location_check),
                     location_eligibility_status=location_check.status,
                     location_eligibility_reason=location_check.reason)
        if location_check.actionability_cap_required:
            match.update(primary_recommendation_eligible=False,
                         actionability_cap_reasons=list(dict.fromkeys(
                             list(match.get('actionability_cap_reasons') or []) + ['location_actionability_cap'])))
    if not conflicts and not location_conflict and not unknowns:
        return match
    assessment = deepcopy(match['affirmative_fit'])
    if conflicts or location_conflict:
        reasons = [c['message'] for c in conflicts] + ([location_check.reason] if location_conflict else [])
        assessment['status'] = 'conflicting'
        assessment['conflicting_requirements'] = list(assessment['conflicting_requirements']) + reasons
        caps = list(match.get('actionability_cap_reasons') or [])
        caps += ['mandatory_language_proficiency_conflict'] if conflicts else []
        caps += ['incompatible_location'] if location_conflict else []
        return dict(match, affirmative_fit=assessment, affirmative_fit_status='conflicting',
                    eligible_for_personalized=False, primary_recommendation_eligible=False,
                    conditional_task_fit=False, preview_section='explore_only',
                    actionability_cap_reasons=list(dict.fromkeys(caps)),
                    primary_admission_reasons=list(dict.fromkeys(list(match.get('primary_admission_reasons') or []) + caps
                        + (['accepted_task_source_location'] if location_conflict else []))),
                    primary_admission_source='accepted_source_eligibility')
    if assessment['status'] != 'supported' or not match.get('eligible_for_personalized', True):
        return match
    assessment['status'] = 'uncertain'
    assessment['unmodeled_requirements'] = list(assessment['unmodeled_requirements']) + [c['message'] for c in unknowns]
    note = 'Your experience is relevant; confirm the requested language level before applying.'
    return dict(match, affirmative_fit=assessment, affirmative_fit_status='uncertain',
                primary_recommendation_eligible=False, conditional_task_fit=True,
                primary_admission_source='accepted_source_language_level',
                source_task_fit=dict(kind='accepted_source_language_level', status='uncertain',
                                     source_reference=ref, conditions=unknowns, candidate_note=note))


def matched_accepted_tasks(profile, row, *, include_transferable=True):
    """Confirmed work, transferable activity, or source-proven beginner interest.

    Domain qualifications remain separate. A generic evaluation phrase in an
    unsupported professional domain does not establish that profession.
    """
    # Legacy callers can still supply sqlite3.Row catalog projections, which
    # contain no prepared accepted-task field.
    if not hasattr(row, 'get'):
        return None
    evidence = row.get('accepted_task_evidence') or {}
    work = profile.get('confirmed_ai_work_evidence') or []
    if evidence.get('version') != TASK_PROJECTION_VERSION:
        return None
    ref = evidence.get('source_reference') or {}
    if any(ref.get(k) != row.get(k) for k in ('job_id', 'canonical_opportunity_id', 'source_slug')):
        return None
    if ref.get('source_url') != row.get('url'):
        return None
    if include_transferable:
        from wahojobs.matching.beginner_access import match_interests
        beginner = match_interests(profile, evidence)
        if beginner:
            return beginner
    if not work:
        if include_transferable:
            from wahojobs.matching.transferable_tasks import match_activities
            return match_activities(profile, evidence)
        return None
    # Specialist clauses stay with the existing domain-specific machinery.
    # This narrow projection establishes general task overlap only, even for
    # profiles mentioning a related domain somewhere in their summary.
    facts = [f for f in evidence['facts'] if not f['professional_domains']]
    if not facts:
        return None
    return dict(source_reference=ref, facts=facts, profile_facts=work)


def apply_task_section_admission(match):
    """Use demonstrated fit as an alternative to the score-only section floor.

    This runs AFTER every existing fit/eligibility/trust guardrail. Numeric
    scores stay unchanged. Source-condition review and typed preferences still
    run before either main or conditional presentation.
    """
    if (match.get('accepted_task_fit')
            and match.get('affirmative_fit_status') == 'supported'
            and match.get('primary_recommendation_eligible') is True
            and match.get('opportunity_trust_status') == 'trusted'
            and not match.get('actionability_cap_reasons')
            and not any((match.get('score_components') or {}).get(k, 0) for k in
                        ('avoid_keyword_penalty', 'quality_gate_penalty', 'specialist_domain_penalty'))
            and not any(r.get('source') == 'title' for r in
                        (match.get('affirmative_fit') or {}).get('required_groups', []))
            and match.get('raw_product_section') == 'explore_only'
            and match.get('effective_product_section') == 'explore_only'
            and match.get('preview_section') == 'explore_only'):
        return dict(match, preview_section='also_worth_reviewing',
                    accepted_task_section_admission=True)
    return match


def needs_accepted_task_comparison(match):
    """Allow exact-source review of a task-supported, unmodeled title only.

    This grants no fit, section admission or action permission. All other
    modeled requirements and non-freshness guardrails must already be clear.
    """
    assessment = match.get('affirmative_fit') or {}
    if (not match.get('accepted_task_fit') or assessment.get('status') != 'uncertain'
            or tuple(assessment.get('unmodeled_requirements') or ()) != ('Title-defining role or specialization',)
            or assessment.get('missing_requirements') or assessment.get('conflicting_requirements')):
        return False
    from scripts.local_product_app import browser_match_rejection_reasons
    return set(browser_match_rejection_reasons(match)) == {'affirmative_fit_not_supported'}


def apply_task_condition_review(match, source, profile, *, background_context=None):
    """Reuse existing source-condition comparisons on the pre-admission pool.

    No general qualification parser: unparsed source qualifications remain
    unassessed and can be offered conditionally, never asserted satisfied.
    Optional criteria and routine screening questions do not become gates.
    """
    # Recomputed source review must not inherit an earlier admission receipt.
    match = {k: v for k, v in match.items() if k != 'accepted_task_pre_review'}
    transferable = (match.get('accepted_task_fit') or {}).get('basis') == 'transferable_activity'
    beginner = (match.get('accepted_task_fit') or {}).get('basis') == 'beginner_interest'
    if transferable or beginner:
        if beginner:
            from wahojobs.matching.beginner_access import bind_confirmed_profile
        else:
            from wahojobs.matching.transferable_tasks import bind_confirmed_profile
        bound = bind_confirmed_profile(match['accepted_task_fit'], profile)
        if bound is None:
            return dict(match, accepted_task_fit=None, accepted_task_section_admission=False,
                        conditional_task_fit=False, primary_recommendation_eligible=False,
                        preview_section='explore_only')
        match = dict(match, accepted_task_fit=bound)
    review_only = needs_accepted_task_comparison(match)
    pre_review = dict(
        review_only=review_only,
        sections={k: match.get(k) for k in ('raw_product_section', 'effective_product_section', 'preview_section')},
        primary_admission_source=match.get('primary_admission_source'),
        primary_admission_reasons=list(match.get('primary_admission_reasons') or []))
    if (not review_only and (not match.get('accepted_task_fit')
            or not (match.get('affirmative_fit_status') == 'supported'
                    or match.get('affirmative_fit_status') == 'uncertain' and match.get('conditional_task_fit')))):
        return match
    from scripts.local_product_app import browser_match_rejection_reasons
    # The existing recent-cache fallback permits freshness-only caps. It must
    # receive the same qualification/location checks as a fresh candidate.
    if not review_only and browser_match_rejection_reasons(match, allow_conditional_task_fit=True):
        return match
    from copy import deepcopy
    from wahojobs.authenticated_card_evidence import prepare_card_evidence, _QUALIFICATION_HEADINGS
    packet = prepare_card_evidence(match, source, profile, background_context=background_context)
    if packet is None:
        # The task packet must not authorize a new section if its current
        # source identity/content cannot also be presented for review.
        if match.get('accepted_task_section_admission'):
            return dict(match, preview_section='explore_only',
                        accepted_task_section_admission=False)
        return match
    match = dict(match, source_qualification_comparisons=packet['comparisons'])
    source_locations = _source_location_checks(packet, profile)
    failed_locations = [r for r in source_locations if r['status'] != 'eligible']
    if failed_locations:
        # A newly recognized task never licenses bypassing an explicit source
        # restriction that the reduced catalog fields did not carry. Reuse the
        # existing location comparator; do not alter an adapter or stored row.
        conflict = any(r['status'] == 'incompatible' for r in failed_locations)
        assessment = deepcopy(match['affirmative_fit'])
        assessment['status'] = 'conflicting' if conflict else 'uncertain'
        field = 'conflicting_requirements' if conflict else 'unmodeled_requirements'
        assessment[field] = list(assessment[field]) + [r['reason'] for r in failed_locations]
        return dict(match, affirmative_fit=assessment, affirmative_fit_status=assessment['status'],
                    primary_recommendation_eligible=False, conditional_task_fit=False,
                    primary_admission_source='accepted_task_source_location',
                    primary_admission_reasons=list(match.get('primary_admission_reasons') or [])
                        + ['accepted_task_source_location'],
                    actionability_cap_reasons=list(match.get('actionability_cap_reasons') or [])
                        + ['incompatible_location' if conflict else 'unconfirmed_location_restriction'],
                    source_task_location_checks=source_locations)
    questions = []
    non_decisive_questions = []
    # The late source-condition path has the confirmed canonical provenance.
    # Reuse the same language comparator for explicitly confirmed firm limits;
    # raw matcher constraints or unconfirmed conversational facts cannot veto.
    from wahojobs.matching.languages import compare_language_condition
    for previous in match.get('source_language_checks') or []:
        ref=previous.get('source_reference') or {}
        if (ref.get('job_id')!=packet['job_id'] or ref.get('source_url')!=packet['url']
                or ref.get('material_content_sha256')!=packet['source_hash']):continue
        checked=compare_language_condition(profile,previous)
        if checked['modality']=='required' and checked['status']=='contradicted':
            questions.append(dict(kind='language',modality='required',status='contradicted',
                supported_parts=[],message=checked['message'],profile_facts=checked['profile_facts'],
                source=dict(quote=checked['quote'],job_id=packet['job_id'],url=packet['url'])))
    for row in packet['comparisons']:
        # Reuse whole-clause modality from presentation. A waiver supplies
        # neither a candidate shortfall nor professional support.
        if row['modality'] == 'not_required':
            continue
        quote = re.sub(r'[*#]', '', row['source']['quote'])
        explicit = any(not term_is_negated(quote.lower(), m.start(), m.end())
                       and not re.search(r'\b(?:not|never)\s*$', quote[:m.start()], re.I)
                       for m in re.finditer(r'\bmust\b|\b(?:job|role|position) requires\b|\brequired\b', quote, re.I))
        modality = 'required' if explicit and row['modality'] != 'unresolved' else row['modality']
        qualification = row['source']['heading'].casefold().rstrip(':') in _QUALIFICATION_HEADINGS
        material = (modality in ('required', 'conflicting')
                    or modality != 'preferred' and (qualification or row['kind'] == 'workload'))
        if material and row['status'] != 'supported':
            from wahojobs.matching.recommendation_policy import condition_materiality
            # An established contradiction cannot be waived by semantic
            # materiality or by positive evidence on another component.
            annotation = condition_materiality(row, source) if row['status'] != 'contradicted' else None
            if annotation is not None and not annotation['admission_decisive']:
                non_decisive_questions.append(dict(row, modality=modality, materiality=annotation,
                    admission_decisive=False))
                continue
            questions.append(dict(row, modality=modality))
    if non_decisive_questions:
        # Questions keep their original unknown state, modality and evidence.
        # This annotation never creates affirmative fit or changes a score.
        match = dict(match, non_decisive_source_questions=non_decisive_questions)
    if not questions:
        return match
    conflicts = [r for r in questions if r['status'] == 'contradicted' and r['modality'] == 'required']
    unsupported_background = [r for r in questions if r['kind'] == 'professional_background'
                              and not r['supported_parts'] and r['modality'] != 'conflicting']
    if unsupported_background and not conflicts:
        # A conditional question must resolve a credible fit, not replace the
        # central profession with generic AI-task overlap. Absence stays unknown;
        # required contradictions are handled first, below, regardless of parts.
        assessment = deepcopy(match['affirmative_fit'])
        assessment['status'] = 'uncertain'
        assessment['missing_requirements'] = list(assessment['missing_requirements']) + [r['source']['quote'] for r in unsupported_background]
        note = 'This role asks for a professional background that your confirmed profile does not establish.'
        return dict(match, affirmative_fit=assessment, affirmative_fit_status=assessment['status'],
                    primary_recommendation_eligible=False, conditional_task_fit=False,
                    preview_section='explore_only', accepted_task_section_admission=False,
                    primary_admission_source='accepted_task_professional_background',
                    primary_admission_reasons=list(dict.fromkeys(list(match.get('primary_admission_reasons') or [])
                        + ['unsupported_source_professional_background'])),
                    source_task_fit=dict(kind='accepted_task_professional_background', status=assessment['status'],
                        source_reference=match['accepted_task_fit']['source_reference'],
                        conditions=unsupported_background,candidate_note=note))
    if review_only and not conflicts:
        # Reaching a comparison is not a new generic admission route. Keep
        # title uncertainty until an established central-background comparison
        # supplies related professional evidence; other questions cannot do it.
        if not any(r['kind'] == 'professional_background' and r['supported_parts']
                   and r['modality'] == 'required' for r in packet['comparisons']):
            return match
    assessment = deepcopy(match['affirmative_fit'])
    conflicts = [r for r in questions if r['status'] == 'contradicted' and r['modality'] == 'required']
    status = 'conflicting' if conflicts else 'uncertain'
    assessment['status'] = status
    # Keep positive task evidence and exact source wording separate from
    # candidate knownness; an unparsed clause does not say the candidate lacks it.
    field = 'conflicting_requirements' if conflicts else 'unmodeled_requirements'
    assessment[field] = list(assessment[field]) + [
        r['message'] or 'Source condition not assessed: ' + r['source']['quote']
        for r in (conflicts or questions)]
    note = ('Your confirmed evaluation or annotation work matches these tasks. '
            'Check the source conditions below before applying.')
    if transferable:
        note = ('Your confirmed activities are relevant to these entry-level tasks. '
                'This does not establish prior professional AI work. Check the remaining source conditions.')
    if beginner:
        note = ('The source establishes beginner access, and its tasks align with your stated work interests. '
                'Check the remaining source conditions.')
    if conflicts:
        note = ('These tasks align with your interests, but a required condition conflicts with your confirmed profile.'
                if beginner else 'Your task experience is relevant, but a required condition conflicts with your confirmed profile.')
    assessment['why_fit_statements'] = [note]
    return dict(match, affirmative_fit=assessment, affirmative_fit_status=status,
                affirmative_fit_why=[note], primary_recommendation_eligible=False,
                accepted_task_pre_review=pre_review,
                primary_admission_source='accepted_task_source_conditions',
                primary_admission_reasons=list(dict.fromkeys(
                    list(match.get('primary_admission_reasons') or []) + ['accepted_task_source_conditions'])),
                conditional_task_fit=not conflicts,
                source_task_fit=dict(kind='accepted_task_conditions', status=status,
                    source_reference=match['accepted_task_fit']['source_reference'],
                    profile_facts=match['accepted_task_fit']['profile_facts'],
                    conditions=questions, candidate_note=note))


def is_conditional_section_candidate(match):
    """Consider an already bounded exploratory representative conditionally.

    This uses only the locally reviewed task/qualification result, not model
    prose. It changes neither that result nor the original section. Other
    conditional routes do not require this professional-background contract.
    """
    from scripts.local_product_app import browser_match_rejection_reasons
    from wahojobs.matching.source_task_fit import is_conditional_task_fit
    pre = match.get('accepted_task_pre_review') or {}
    task_fit = match.get('accepted_task_fit') or {}
    reference = task_fit.get('source_reference') or {}
    reviewed_reference = (match.get('source_task_fit') or {}).get('source_reference') or {}
    assessment = match.get('affirmative_fit') or {}
    required = [r for r in match.get('source_qualification_comparisons') or [] if r['modality'] == 'required']
    backgrounds = [r for r in required if r['kind'] == 'professional_background']
    return bool(
        pre.get('review_only') is True and task_fit
        and all(pre.get('sections', {}).get(k) == 'explore_only' and match.get(k) == 'explore_only'
                for k in ('raw_product_section', 'effective_product_section', 'preview_section'))
        and all(reference.get(k) == match.get(k) for k in ('job_id', 'canonical_opportunity_id', 'source_slug'))
        and reference.get('source_url') == match.get('url') and reference == reviewed_reference
        and is_conditional_task_fit(match)
        and match.get('primary_recommendation_eligible') is False
        and match.get('primary_admission_source') == 'accepted_task_source_conditions'
        and backgrounds and all(r['supported_parts'] and r['status'] != 'contradicted' for r in backgrounds)
        and all(r['source']['job_id'] == match.get('job_id') and r['source']['url'] == match.get('url')
                and r['source']['source_hash'] == reference.get('material_content_sha256') for r in required)
        # Use the group's status, never veto an unsuccessful qualifying route.
        and not any(r['status'] == 'contradicted' for r in required)
        and not assessment.get('missing_requirements') and not assessment.get('conflicting_requirements')
        and all(r['status'] == 'eligible' for r in match.get('source_task_location_checks') or [])
        and not any(r.get('modality') == 'required' and r.get('status') == 'contradicted'
                    for r in match.get('source_language_checks') or [])
        and not browser_match_rejection_reasons(match, allow_conditional_task_fit=True)
        and match.get('opportunity_trust_status') == 'trusted'
        and not match.get('actionability_cap_reasons')
        and not any(match.get(k) for k in ('professional_domain_hard_gate_applied',
            'specialized_actionability_cap_applied', 'location_actionability_cap_applied',
            'preview_domain_hard_gate_applied', 'raw_professional_domain_hard_gate_applied'))
        and not any((match.get('score_components') or {}).get(k, 0)
                    for k in ('avoid_keyword_penalty', 'quality_gate_penalty', 'specialist_domain_penalty')))


def _source_location_checks(packet, profile):
    """Pass explicit applicant-location wording to the existing location gate.

    This only guards task-based admission, on its bounded candidate pool. It
    grants no new permission, interprets no employer/citizenship/timezone data,
    and leaves unsupported compound wording unresolved.
    """
    from wahojobs.matching.locations import location_eligibility
    from wahojobs.candidate_source_display import plain
    statements = []
    for line in packet['text'].splitlines():
        quote = plain(line).strip().lstrip('- ').strip()
        field = re.fullmatch(r'((?:Applicant |Candidate )?Location):\s*(.+)', quote, re.I)
        if field and (field[1].casefold() != 'location' or
                      re.search(r'\b(?:only|onsite|on-site|hybrid|must|required)\b', field[2], re.I)):
            statements.append((quote, field[2], 'source location field'))
    for row in packet['comparisons']:
        if row['modality'] == 'preferred':
            continue
        quote = plain(row['source']['quote']).strip()
        place = re.match(
            r'^(?:(?:Applicants|Candidates|Contributors|Workers) (?:must|are required to) |Must )?'
            r'(?:be )?(?:currently )?(?:based|located|residing|living|reside|live) in (.+)', quote, re.I)
        if place:
            statements.append((quote, place[1], row['source']['block_reference']))
    results = []
    for quote, value, ref in dict.fromkeys(statements):
        value = re.split(r'(?<=[.!])\s+', value, maxsplit=1)[0].rstrip('.,')
        if re.search(r'\b(?:headquarters|employer|customers?|markets?|timezone|time zone|citizenship|nationality)\b', value, re.I):
            continue
        # Retain only the location sentence; do not borrow a country from a
        # later statement about another dimension or a prohibited exception.
        ambiguous = bool(re.search(r'\b(?:not|except|unless|if|preferred|timezone|time zone|citizen|nationality|authorization|visa)\b', value, re.I))
        if ambiguous:
            result = dict(status='unknown', reason='The source applicant-location condition needs clarification.')
        else:
            checked = location_eligibility({'country': profile.get('location', {}).get('country', '')},
                                           {'location': value})
            if (checked.status in ('not_applicable', 'unknown') and not checked.actionability_cap_required
                    and not re.search(r'\b(?:onsite|on-site|hybrid|must|required)\b', value, re.I)):
                continue  # Remote/missing wording is not a country requirement.
            result = dict(status=checked.status, reason=checked.reason)
        results.append(dict(result, quote=quote, source_reference=ref, job_id=packet['job_id'],
                            source_hash=packet['source_hash']))
    return results

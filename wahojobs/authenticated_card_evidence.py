"""Source-faithful presentation for already-selected authenticated cards.

No scoring, eligibility evaluation, title inference or persistent extraction.
Heading boundaries format quotations; their contents are never turned into gates.
"""
from html import escape
import json
import re

from wahojobs.crawler.provider_details import DETAIL_KEY
from wahojobs.opportunity_enrichment import source_body_paragraphs


_QUALIFICATION_HEADINGS = {
    'required', 'requirements', 'required qualifications', 'minimum qualifications',
    'requirements (must have)', 'preferred (nice to have)',
    'required skills and qualifications',
    'qualifications', 'key qualifications', 'education & experience',
    'ideal qualifications', 'preferred', 'preferred qualifications',
    'nice to have', 'who you are', "what we're looking for", 'what we are looking for',
    'what we’re looking for',
}
_TERMS_HEADINGS = {'engagement', 'role details', 'more about the opportunity',
                   'commitment', 'equipment', 'equipment requirements',
                   'screening questions', 'application screening questions', 'additional requirements'}
# Plain headings retained by the accepted detail formatter. Formatting only:
# boundaries prevent later compensation/screening text inheriting a qualification label.
_SOURCE_HEADINGS = {'scope of work', 'compensation structure',
                    'start timeline & availability', 'application screening questions',
                    'other published fields (read alongside the description)',
                    'other published fields (not additional applicant requirements)'}


def load_card_sources(connection, matches):
    """One bounded exact-ID read, after the visible list has been selected."""
    ids = sorted({m['job_id'] for m in matches if type(m.get('job_id')) is int})
    if not ids:
        return {}
    cursor = connection.execute(f"""
        SELECT j.id AS job_id, j.canonical_opportunity_id, j.external_id, j.source_hash,
               j.url, j.commitment, j.location, c.slug AS source_slug,
               sc.external_id AS content_external_id, sc.provider AS content_provider, sc.source_url,
               sc.body, sc.body_format, sc.metadata_json, sc.last_captured_at,
               sc.material_content_sha256
        FROM jobs j JOIN companies c ON c.id=j.company_id
        LEFT JOIN job_source_contents sc ON sc.job_id=j.id
        WHERE j.id IN ({','.join('?' for _ in ids)})
    """, ids)
    columns = [item[0] for item in cursor.description]
    sources = {row['job_id']: row for row in (dict(zip(columns, r)) for r in cursor)}
    from wahojobs.source_clause_materiality import attach_current_annotations
    return attach_current_annotations(connection, sources)


def _blocks(text):
    """Preserve whole source blocks and labels, including alternatives/negation."""
    blocks, heading, lines = [], 'Source wording', []
    for line in text.splitlines():
        stripped = line.strip()
        label = stripped.strip('#*: ').strip()
        is_heading = (bool(re.fullmatch(r'\*\*[^*\n]{1,110}\*\*:?|#{1,6} .{1,110}', stripped))
                      or label.casefold() in _QUALIFICATION_HEADINGS | _TERMS_HEADINGS | _SOURCE_HEADINGS)
        if is_heading:
            if lines:
                blocks.append({'heading': heading, 'text': '\n'.join(lines).strip()})
            heading, lines = label, []
        elif stripped or lines:
            lines.append(line)
    if lines:
        blocks.append({'heading': heading, 'text': '\n'.join(lines).strip()})
    return [dict(b, reference=f'source block {i}') for i, b in enumerate(blocks, 1)]


def _source_text(source, *, include_structured_lists=True):
    metadata = json.loads(source.get('metadata_json') or '{}')
    if not isinstance(metadata, dict):
        raise ValueError('invalid_source_metadata')
    detail = metadata.get(DETAIL_KEY, {})
    if source.get('source_slug') == 'alignerr':
        # The accepted body is employer wording. Cached display_text also contains
        # our historical field labels; never feed those back as employer prose.
        text = source.get('body') or ''
    elif (isinstance(detail, dict) and detail.get('external_id') == source['external_id']
            and detail.get('provider') == source['source_slug']
            and detail.get('url') == source['url']
            and isinstance(detail.get('display_text'), str)):
        text = detail['display_text']
    else:
        text = source.get('body') or ''
    # Some accepted feeds label HTML as text. The same safe paragraph reader
    # must feed task recognition, conditions and presentation.
    if source.get('body_format') == 'text/html' or re.match(r'\s*<(?:p|div|h[1-6]|ul|section)\b', text, re.I):
        text = '\n\n'.join(source_body_paragraphs(text, 'text/html'))
    if include_structured_lists and isinstance(metadata.get('lists'), list):
        for block in metadata['lists']:
            if not isinstance(block, dict) or not all(isinstance(block.get(k), str) for k in ('text', 'content')):
                continue
            paragraphs = source_body_paragraphs(block['content'], 'text/html')
            if paragraphs and not all(p in text for p in paragraphs):
                text += '\n\n## ' + block['text'] + '\n\n' + '\n\n'.join(paragraphs)
    return text


def prepare_card_evidence(match, source, profile, *, include_item_experience=False,
                          conditional_placement=False, background_context=None):
    if not source or any(source.get(k) != match.get(k) for k in
                         ('job_id', 'canonical_opportunity_id', 'url', 'source_slug')):
        return None
    if source.get('content_external_id') != source.get('external_id'):
        return None
    # An accepted body from another destination must not decorate this variant.
    if source.get('source_url') != source.get('url'):
        return None
    try:
        text = _source_text(source)
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    blocks = _blocks(text)
    paragraphs = [b for b in blocks if b['text']]
    kind, kind_quote = 'Opportunity type not established', ''
    for b in paragraphs:
        future_pool = (re.search(r'\b(?:join|become part of) (?:our |an? )?(?:exclusive )?talent pool\b', b['text'], re.I)
                       and re.search(r'\b(?:upcoming|future) (?:roles|projects|opportunities)\b', b['text'], re.I)
                       and not re.search(r'\b(?:not|never|don.t)\b', b['text'], re.I))
        if future_pool or re.search(r'\b(?:not a specific job posting|open application for future (?:contract )?opportunities)\b', b['text'], re.I):
            kind, kind_quote = 'Talent network — future consideration', b['text']
            break
    if not kind_quote:
        for b in paragraphs:
            if (re.search(r'\b(?:recruit(?:ing)? on an ongoing basis|continuously recruiting|rolling basis)\b', b['text'], re.I)
                    and not re.search(r'\bnot\b', b['text'], re.I)):
                kind, kind_quote = 'Ongoing recruiting', b['text']
                break
    if not kind_quote:
        for b in paragraphs:
            if (re.search(r'\b(?:we are seeking|we.re seeking|is hiring|we are hiring)\b', b['text'], re.I)
                    and not re.search(r'\bnot (?:currently )?(?:hiring|seeking)\b', b['text'], re.I)):
                kind, kind_quote = 'Advertised role/project', b['text']
                break
    domains = [d for d in match.get('matched_core_domains', []) if isinstance(d, str)
               and re.search(r'(?<!\w)' + re.escape(d) + r'(?!\w)', text, re.I)]
    facts = (profile.get('education', {}).get('degrees', [])
             + profile.get('experience', {}).get('specialties', []))
    fact = next((f for f in facts if isinstance(f, str)
                 and any(d.casefold() in f.casefold() for d in domains)), '')
    reason = (f'Your profile lists {fact}. The existing comparison found {", ".join(domains[:2])} overlap.'
              if fact else '')
    from wahojobs.candidate_source_display import plain, pay_facts
    metadata = json.loads(source.get('metadata_json') or '{}')
    detail = metadata.get(DETAIL_KEY)
    detail = detail if isinstance(detail, dict) else {}
    from wahojobs.source_detail_presentation import bound_alignerr_detail, alignerr_location_provenance, alignerr_other_fields
    if source.get('source_slug') == 'alignerr':
        detail = bound_alignerr_detail(source, detail) or {}
    record = detail.get('record')
    record = record if isinstance(record, dict) else {}
    task_headings = {"what you'll do", "what you’ll do", 'scope of work', 'responsibilities',
                     'key responsibilities'}
    task = next((b for b in blocks if (b['heading'].casefold().rstrip(':') in task_headings or
                  re.fullmatch(r'about .+ projects', b['heading'], re.I))), None)
    if task is None:
        task = next((b for b in blocks if b['heading'].casefold() == 'role overview'), None)
    summary = ''
    if task:
        lines = [plain(re.sub(r'^\s*(?:[-*+] |\d+[.)] )', '', line))
                 for line in task['text'].splitlines() if line.strip()]
        bullets = [plain(re.sub(r'^\s*[-*+] ', '', line))
                   for line in task['text'].splitlines() if re.match(r'^\s*[-*+] ', line)]
        chosen = bullets[:1] if bullets else lines[:1]
        summary = ' '.join(line.rstrip('.') + '.' for line in chosen)
    # A generic subject overlap adds nothing to a subject-specific title/task.
    # Retain comparison evidence in the packet, but do not repeat it on every card.
    conditions = [b for b in blocks if b['heading'].casefold().rstrip(':') in
                  _QUALIFICATION_HEADINGS | _TERMS_HEADINGS]
    workload_lines = [plain(line).lstrip('- ').strip() for line in text.splitlines()
                      if re.search(r'\b(?:hours|hrs)\b.{0,20}\b(?:week|weekly)\b', line, re.I)
                      and len(line.strip()) <= 240]
    workload = next((line for line in workload_lines if re.search(r'\d', line)), '')
    workload = re.sub(r'^(?:expected )?commitment:\s*', '', workload, flags=re.I)
    hours = re.search(r'\b\d+(?:\s*[-–]\s*\d+)?\+?\s*(?:hours|hrs)\+?\s*(?:/|per )\s*week\b', workload, re.I)
    short_workload = hours.group() if hours else workload
    if short_workload and re.search(r'\btypical', workload, re.I):
        short_workload = 'Typically ' + short_workload
    if re.search(r'\b(?:or|if|not|up to|at most|at least|approximately|minimum|maximum)\b|\?', workload, re.I):
        # A short numeric snippet must not erase alternatives or qualifiers.
        short_workload = workload
    if '?' in workload:
        # Keep the complete screening question (including any OR alternative)
        # in the disclosure; a question is not a settled contract term.
        short_workload = 'Weekly commitment to confirm'
        if not any(workload in plain(b['text']) for b in conditions):
            conditions.append({'heading': 'Workload question', 'text': workload,
                               'reference': 'source workload wording'})
    country = profile.get('location', {}).get('country') or ''
    location = match.get('location_eligibility_status', 'unknown')
    geography = ({'eligible': '',
                  'incompatible': 'The applicant-location restriction conflicts with your profile.'}.get(location)
                 if location in ('eligible', 'incompatible') else
                 (f"Eligibility from {country} needs confirmation." if country else 'Applicant-location eligibility isn’t specified.'))
    location_context = _applicant_location_context(match, source, detail)
    published_location = alignerr_location_provenance(source, detail)
    opaque_location = (bool(published_location and published_location['generic_country_tag'])
                       or _opaque_posting_location(record.get('location'), match))
    source_place_note = (_differing_source_locations(source, detail, include_listing=not opaque_location)
                         if location == 'unknown' and not location_context['applicant'] else '')
    if source_place_note:
        geography = source_place_note + ' ' + geography
    pay = pay_facts(metadata, text)
    # Formatting of explicit arrangement labels is separate from eligibility.
    arrangement = ''
    for line in text.splitlines():
        line = plain(line).lstrip('- ').strip()
        if re.fullmatch(r'Location:\s*Remote', line, re.I):
            arrangement = 'Remote'; break
    if not arrangement and str(source.get('location') or '').casefold() == 'remote':
        arrangement = 'Remote'
    engagement = next((plain(line).lstrip('- ').split(':', 1)[1].strip()
                       for line in text.splitlines()
                       if re.match(r'^(?:- )?(?:Type|Role Type):\s*(?:Hourly Contract|Contractor|Contract|Part-time|Full-time)\s*$', plain(line), re.I)), '')
    if not engagement and (source.get('commitment') or '').lower() in ('part-time', 'full-time', 'contract'):
        engagement = source['commitment'].capitalize()
    if not engagement and re.match(r'(?:part-time|full-time)\b', workload, re.I):
        engagement = workload.split(',')[0].capitalize()
    caveats = [geography] if geography else []
    task_note = (match.get('source_task_fit') or {}).get('candidate_note')
    if task_note:
        caveats.append(task_note)
    caveats += pay['notes']
    commitment = source.get('commitment') or ''
    if (commitment.casefold() in ('full-time', 'part-time') and
            re.search(r'\b' + ('part-time' if commitment.casefold() == 'full-time' else 'full-time') + r'\b', text, re.I)):
        caveats.append(f'The listing says {commitment}; the description gives different workload terms. Confirm the schedule.')
    fields = []
    for label, value in [('Pay', pay['label'] or 'Confirm with employer'), ('Work arrangement', arrangement),
                         ('Workload', short_workload), ('Engagement', engagement)]:
        if value:
            fields.append((label, value))
    duration = next((plain(line).lstrip('- ').split(':', 1)[1].strip()
                     for line in text.splitlines() if re.match(r'^(?:- )?Duration:', plain(line), re.I)), '')
    if duration:
        fields.append(('Duration', duration))
    # An opaque source location does not establish residence permission.
    source_location = record.get('location')
    if (not source_place_note and isinstance(source_location, str) and source_location
            and arrangement == 'Remote' and source_location.lower() != 'remote'
            and all(detail.get(k) == source.get(s) for k, s in
                    [('provider', 'source_slug'), ('external_id', 'external_id'), ('url', 'url')])):
        if geography in caveats:
            caveats.remove(geography)
        if not location_context['applicant']:
            location_context['other'] = _other_location_wording(source_location)
        if geography and geography not in caveats:
            caveats.append(geography)
    # Keep the attributed value in the packet; omit only safely recognized bare
    # posting metadata from guidance, never potentially meaningful free text.
    location_context['omit_opaque_other'] = bool(location_context['other'] and opaque_location)
    location_context['published_field'] = published_location
    location_context['other_fields'] = alignerr_other_fields(source, detail)
    if location_context['published_field']:
        # One attributed presentation shared by cards and item details. Independent
        # applicant comparisons and cautions above retain their existing meaning.
        location_context['other'] = ''
    elif location_context['other'] and isinstance(source_location, str):
        # Older accepted detail packets can carry an attributed location field
        # outside the provider-specific published-field projection. Promote its
        # wording only with the accepted body's existing exact identity binding.
        # Absence of an extracted applicant invitation does not invalidate a
        # raw field. In that case the retained display body must match exactly;
        # an existing but invalid preparation never falls back to that path.
        from hashlib import sha256
        from wahojobs.source_capture import normalize_source_body
        support = detail.get('applicant_location_support')
        accepted_body = normalize_source_body(source.get('body')) or ''
        body_bound = (bool(accepted_body) and
                      normalize_source_body(detail.get('display_text')) == accepted_body
                      if support is None else
                      isinstance(support, dict) and support.get('version') == 1
                      and support.get('body_sha256') == sha256(accepted_body.encode()).hexdigest())
        if (body_bound and all(detail.get(k) == source.get(s) for k, s in
                        [('provider', 'source_slug'), ('external_id', 'external_id'), ('url', 'url')])):
            location_context['other_field'] = dict(value=source_location,
                generic_country_tag=bool(opaque_location), source_field='accepted_detail.record.location',
                job_id=source['job_id'], external_id=source['external_id'], source_url=source['url'],
                material_content_sha256=source['material_content_sha256'])
    language_notes = []
    for check in match.get('source_language_checks') or []:
        ref = check.get('source_reference') or {}
        if (ref.get('job_id') == source['job_id'] and ref.get('source_url') == source['url']
                and ref.get('material_content_sha256') == source['material_content_sha256']
                and check.get('modality') in ('required', 'unresolved')
                and check.get('status') in ('not_established', 'unresolved')
                and isinstance(check.get('message'), str)):
            language_notes.append(check['message'])
    caveats.extend(note for note in language_notes if note not in caveats)
    packet = {'job_id': source['job_id'], 'url': source['url'], 'external_id': source['external_id'],
            'source_hash': source['material_content_sha256'], 'captured_at': source['last_captured_at'],
            'reason': reason, 'task': task, 'summary': summary, 'conditions': conditions, 'blocks': blocks,
            'kind': kind, 'kind_quote': kind_quote, 'geography': geography, 'text': text,
            'workload': workload, 'listing_commitment': commitment, 'facts': fields,
            'pay': pay, 'caveats': caveats, 'language_notes': language_notes,
            'location_context': location_context}
    from wahojobs.candidate_condition_comparisons import compare_conditions
    if source.get('professional_source_binding'):
        from wahojobs.professional_background_semantics import current_source_binding
        binding = current_source_binding(source)
        if binding is not None:
            packet['professional_source_binding'] = binding
    packet['comparisons'] = compare_conditions(packet, profile, include_item_experience=include_item_experience,
                                               background_context=background_context)
    from wahojobs.matching.recommendation_policy import condition_materiality
    packet['condition_materialities'] = [condition_materiality(row, source) for row in packet['comparisons']]
    if conditional_placement:
        packet['placement_explanation'] = _conditional_source_explanation(match, packet)
    from wahojobs.candidate_decision import attach_decision
    packet['task_fit_note'] = task_note
    return attach_decision(packet, match, profile=profile)


def _conditional_source_explanation(match, packet):
    """Present recorded decisions, never classify or reassess source clauses."""
    from copy import deepcopy
    review = match.get('source_task_fit') or {}
    if (match.get('primary_recommendation_eligible') is not False
            or match.get('conditional_task_fit') is not True
            or match.get('primary_admission_source') != 'accepted_task_source_conditions'
            or review.get('kind') != 'accepted_task_conditions'
            or review.get('status') != 'uncertain'):
        return None
    def same_source(ref):
        return (ref.get('job_id') == packet['job_id']
                and ref.get('external_id') == packet['external_id']
                and ref.get('source_url') == packet['url']
                and ref.get('material_content_sha256') == packet['source_hash'])
    if not same_source(review.get('source_reference') or {}):
        return None
    questions = review.get('conditions') or []
    if not questions:
        return None
    for row in questions:
        # All causes must still correspond to this exact accepted clause. Do not
        # silently shorten a stale/mismatched explanation or soften a conflict.
        if (row.get('status') not in ('unresolved', 'not_established')
                or row.get('modality') in ('preferred', 'conflicting', 'not_required')
                or row.get('admission_decisive') is False
                or not any(c['source'] == row.get('source') and c['status'] == row['status']
                           for c in packet['comparisons'])):
            return None
    languages = []
    for check in match.get('source_language_checks') or []:
        if (check.get('status') != 'supported'
                or not same_source(check.get('source_reference') or {})
                or not any(check.get('quote') == q['source']['quote'] for q in questions)):
            continue
        if check.get('levels') == ['native']:
            label = (' or ' if check.get('operator') == 'any_of' else ' and ').join(
                language.title() for language in check['languages'])
            message = f'Your stated native {label} supports the language component.'
        else:
            message = check.get('message')
        if message:
            message += ' The other parts of the quoted qualification still need assessment.'
            if not any(item['message'] == message for item in languages):
                linked = [q['source'] for q in questions if q['source']['quote'] == check.get('quote')]
                # Do not guess a clause position when identical wording repeats.
                if len(linked) == 1:
                    languages.append(dict(message=message, comparison=deepcopy(check), source=deepcopy(linked[0])))
    count = len(questions)
    summary = (f'{count} job-specific ' + ('point still needs' if count == 1 else 'points still need')
               + ' assessment against your profile. '
               + ('This contributes' if count == 1 else 'These contribute')
               + ' to this opportunity being shown as a possibility; other aspects of the match may also need review.')
    return dict(summary=summary, conditions=deepcopy(questions), language_support=languages)


def render_placement_explanation(evidence):
    reason = (evidence or {}).get('placement_explanation')
    if not reason:
        return ''
    points = []
    for row in reason['conditions']:
        ref = row['source']
        points.append('<li data-source-reference="' + escape(
            f"{ref['block_reference']}:line {ref['line']}", quote=True) + '">'
            + escape(ref['quote'])
            + ("<p class='candidate-note'>" + escape(row['message']) + '</p>' if row['message'] else '')
            + ''.join("<p class='candidate-note'>" + escape(item['message']) + '</p>'
                      for item in reason['language_support'] if item.get('source') == ref)
            + '</li>')
    return ("<div class='candidate-placement-reason'><h4>Why this is a possibility</h4>"
            + '<p>' + escape(reason['summary']) + '</p><ul>' + ''.join(points) + '</ul>'
            + '</div>')


def render_original_qualifications(blocks):
    """Native disclosure; callers retain comparisons and independent warnings."""
    if not blocks:
        return ''
    return ("<details class='candidate-original-qualifications'><summary>Original employer qualifications</summary>"
            + "<div class='source-description'>" + blocks + '</div></details>')


def _other_location_wording(value):
    return f'Source location field: “{value}”'


def _opaque_posting_location(value, match):
    """Display selection only, using the existing exact country vocabulary."""
    from wahojobs.profiles.countries import normalize_country
    try:
        normalize_country(value)
    except ValueError:
        return False
    conditions = (list(match.get('applicant_country_requirements') or [])
                  + list((match.get('accepted_eligibility_evidence') or {}).get('country_conditions', [])))
    # Preserve the field conservatively when existing evidence establishes a
    # requirement or conflict. Missing semantics do not classify arbitrary text.
    return (match.get('location_eligibility_status') != 'incompatible'
            and all(c.get('modality') == 'invitation' and not c.get('unresolved') and not c.get('source_conflict')
                    and not c.get('ambiguous_statement') for c in conditions))


def _applicant_location_context(match, source, detail):
    """Display prepared invitations, never derive permission from posting metadata."""
    from hashlib import sha256
    from wahojobs.source_capture import normalize_source_body
    from wahojobs.profiles.countries import normalize_country
    result = dict(applicant='', other='', references=[])
    if any(detail.get(k) != source.get(s) for k, s in
           [('provider', 'source_slug'), ('external_id', 'external_id'), ('url', 'url')]):
        return result
    prepared = detail.get('applicant_location_support') or {}
    if (prepared.get('version') != 1 or prepared.get('body_sha256') !=
            sha256((normalize_source_body(source.get('body')) or '').encode()).hexdigest()):
        return result
    clauses = prepared.get('clauses') or []
    # Do not let an invitation soften a restrictive, ambiguous or conflicting
    # statement, including a conflict found by another existing comparison.
    if (not clauses or match.get('location_eligibility_status') == 'incompatible'
            or any(c.get('modality') != 'invitation' or c.get('mode') != 'allow'
                   or c.get('dimension') != 'location' or c.get('unresolved')
                   or c.get('source_conflict') or c.get('ambiguous_statement')
                   or not c.get('source_quote') or not c.get('source_field') for c in clauses)
            or any(c.get('modality') != 'invitation' for c in
                   (match.get('accepted_eligibility_evidence') or {}).get('country_conditions', []))):
        return result
    countries = list(dict.fromkeys(country for c in clauses for country in c['countries']))
    if not countries:
        return result
    result['applicant'] = ('The description explicitly mentions applicants based in '
                           + ', '.join(countries) + '.')
    result['references'] = [dict(source_url=source['url'], job_id=source['job_id'],
                                material_content_sha256=source['material_content_sha256'],
                                source_field=c['source_field'], source_quote=c['source_quote'])
                            for c in clauses]
    record = detail.get('record') or {}
    value = record.get('location')
    if isinstance(value, str) and value and value.casefold() != 'remote':
        try:
            agrees = normalize_country(value) in countries
        except ValueError:
            agrees = False
        if not agrees:
            result['other'] = _other_location_wording(value)
    return result


def render_location_context(evidence):
    from wahojobs.source_detail_presentation import render_location_provenance, render_other_fields
    context = (evidence or {}).get('location_context') or {}
    primary, secondary = context.get('applicant'), context.get('other')
    return (("<p><strong>Applicant location:</strong> " + escape(primary) + '</p>' if primary else '')
            + ("<p class='candidate-note'><strong>Other location information from the source:</strong> "
               + escape(secondary) + '</p>' if secondary and not context.get('omit_opaque_other') else '')
            + render_location_provenance(context.get('published_field'))
            + render_other_fields(context.get('other_fields')))


def _differing_source_locations(source, detail, *, include_listing=True):
    """Explain differing fields from this exact accepted detail, not eligibility.

    A structured city/country must also occur in the original description.
    Neither headquarters nor incidental customer geography becomes residence.
    No city inference, cross-variant borrowing or new country gate occurs here.
    """
    from wahojobs.profiles.countries import COUNTRY_BY_CODE, normalize_country
    from wahojobs.matching.source_geography import _UNRELATED
    if any(detail.get(key) != source.get(source_key) for key,source_key in
           [('external_id','external_id'),('url','url'),('provider','source_slug')]):
        return ''
    record = detail.get('record')
    if not isinstance(record,dict):
        return ''
    country = COUNTRY_BY_CODE.get(str(record.get('countryCode') or '').upper())
    try:
        listing = normalize_country(record.get('location'))
    except (ValueError, TypeError):
        return ''
    if not country or listing == country:
        return ''
    body = source.get('body') or ''
    if source.get('body_format') == 'text/html':
        body = '\n\n'.join(source_body_paragraphs(body, 'text/html'))
    paragraphs = [p for p in re.split(r'\n\s*\n', body) if re.search(r'(?<!\w)'+re.escape(country)+r'(?!\w)',p,re.I)
                  and re.search(r'\b(?:speakers|professionals|applicants|based|looking for|role)\b',p,re.I)
                  and (not _UNRELATED.search(p) or re.search(
                      r'\b(?:speakers|professionals|applicants|candidates|workers) '
                      r'(?:(?:based|located) )?(?:in|across|throughout) '+re.escape(country)+r'\b',p,re.I))]
    if not paragraphs:
        return ''
    city = record.get('city')
    place = (city + ', ' + country if isinstance(city,str) and any(
        re.search(r'(?<!\w)'+re.escape(city)+r'(?!\w)',p,re.I) for p in paragraphs) else country)
    # A city can occur in another applicant paragraph, e.g. a Manila overview
    # followed by an explicitly nationwide Philippines recruitment sentence.
    if isinstance(city,str) and re.search(r'\bbased in '+re.escape(city)+r'\b',body,re.I):
        place = city + ', ' + country
    return (f'The description mentions {place}; the listing says {listing}.' if include_listing
            else f'The description mentions {place}.')


def render_conditions(evidence, card_id):
    from wahojobs.candidate_decision import render_assessment
    if not evidence or not evidence['conditions']:
        return ''
    return (f"<details class='candidate-conditions card-source-disclosure'><summary id='{escape(card_id)}-source-summary'>"
            "Qualifications &amp; conditions</summary><div class='source-description'>"
            + render_assessment(evidence) + '</div></details>')


def render_opportunity_kind(evidence):
    kind = (evidence or {}).get('kind')
    html = (f"<p class='candidate-kind'>{escape(kind)}</p>"
            if kind in ('Talent network — future consideration', 'Ongoing recruiting') else '')
    if kind == 'Talent network — future consideration':
        html += "<p class='candidate-note'>Join for future projects; this is not a specific job posting.</p>"
    return html


def render_card_evidence(evidence, card_id, *, profile_return_to=None):
    from wahojobs.candidate_decision import render_reasons, render_material_warnings
    if evidence is None:
        return "<p class='candidate-note'>Full requirements aren’t available in the saved listing. Check the source before applying.</p>"
    kind_html = render_opportunity_kind(evidence)
    summary = evidence.get('summary') or ''
    summary = summary if len(summary) <= 220 else summary[:217].rstrip() + '…'
    return (f"<section class='card-evidence' data-source-variant='{evidence['job_id']}'>"
            + kind_html + ("<p class='candidate-overview'>" + escape(summary) + '</p>' if summary else '')
            + render_reasons(evidence)
            + render_material_warnings(evidence, compact=True) + '</section>')

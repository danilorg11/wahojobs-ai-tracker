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
    'qualifications', 'ideal qualifications', 'preferred', 'preferred qualifications',
    'nice to have', 'who you are', "what we're looking for", 'what we are looking for',
}
_TERMS_HEADINGS = {'engagement', 'role details', 'more about the opportunity',
                   'commitment', 'equipment', 'equipment requirements',
                   'screening questions', 'additional requirements'}
# Plain headings retained by the accepted detail formatter. Formatting only:
# boundaries prevent later compensation/screening text inheriting a qualification label.
_SOURCE_HEADINGS = {'scope of work', 'compensation structure',
                    'start timeline & availability', 'application screening questions',
                    'other published fields (read alongside the description)'}


def load_card_sources(connection, matches):
    """One bounded exact-ID read, after the visible list has been selected."""
    ids = sorted({m['job_id'] for m in matches if type(m.get('job_id')) is int})
    if not ids:
        return {}
    cursor = connection.execute(f"""
        SELECT j.id AS job_id, j.canonical_opportunity_id, j.external_id,
               j.url, j.commitment, c.slug AS source_slug,
               sc.external_id AS content_external_id, sc.source_url,
               sc.body, sc.body_format, sc.metadata_json, sc.last_captured_at,
               sc.material_content_sha256
        FROM jobs j JOIN companies c ON c.id=j.company_id
        LEFT JOIN job_source_contents sc ON sc.job_id=j.id
        WHERE j.id IN ({','.join('?' for _ in ids)})
    """, ids)
    columns = [item[0] for item in cursor.description]
    return {row['job_id']: row for row in (dict(zip(columns, r)) for r in cursor)}


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
        elif stripped:
            lines.append(line)
    if lines:
        blocks.append({'heading': heading, 'text': '\n'.join(lines).strip()})
    return [dict(b, reference=f'source block {i}') for i, b in enumerate(blocks, 1)]


def _source_text(source):
    metadata = json.loads(source.get('metadata_json') or '{}')
    if not isinstance(metadata, dict):
        raise ValueError('invalid_source_metadata')
    detail = metadata.get(DETAIL_KEY, {})
    if (isinstance(detail, dict) and detail.get('external_id') == source['external_id']
            and detail.get('provider') == source['source_slug']
            and detail.get('url') == source['url']
            and isinstance(detail.get('display_text'), str)):
        return detail['display_text']
    body = source.get('body') or ''
    return ('\n\n'.join(source_body_paragraphs(body, 'text/html'))
            if source.get('body_format') == 'text/html' else body)


def prepare_card_evidence(match, source, profile):
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
        if re.search(r'\b(?:not a specific job posting|open application for future (?:contract )?opportunities)\b', b['text'], re.I):
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
    task = next((b for b in blocks if b['heading'].casefold() not in _QUALIFICATION_HEADINGS | _TERMS_HEADINGS), None)
    conditions = [b for b in blocks if b['heading'].casefold() in _QUALIFICATION_HEADINGS | _TERMS_HEADINGS]
    # A short source quote, not a normalized schedule or candidate verdict.
    workload_lines = [line.strip() for b in blocks for line in b['text'].splitlines()
                      if re.search(r'\b(?:hours|hrs)\b.{0,20}\b(?:week|weekly)\b', line, re.I)
                      and len(line.strip()) <= 240]
    workload = next((line for line in workload_lines if re.search(r'\d', line)),
                    workload_lines[0] if workload_lines else '')
    location = match.get('location_eligibility_status', 'unknown')
    geography = ({'eligible': 'The existing geographic check supports your location; other conditions remain unassessed.',
                  'incompatible': 'The existing geographic check found a source/profile conflict.'}.get(location)
                 or 'Applicant-location eligibility is unresolved; remote does not establish worldwide eligibility.')
    return {'job_id': source['job_id'], 'url': source['url'], 'external_id': source['external_id'],
            'source_hash': source['material_content_sha256'], 'captured_at': source['last_captured_at'],
            'reason': reason, 'task': task, 'conditions': conditions, 'blocks': blocks,
            'kind': kind, 'kind_quote': kind_quote, 'geography': geography,
            'workload': workload, 'listing_commitment': source.get('commitment') or ''}


def render_card_evidence(evidence, card_id):
    e = lambda value: escape(str(value or ''), quote=True)
    if evidence is None:
        return ("<section class='card-evidence'><h4>Why this appeared</h4>"
                "<p>A specific profile-to-task connection cannot be substantiated from the available source evidence.</p>"
                "<h4>What to check before applying</h4><p>Source conditions and opportunity type remain unresolved. "
                "Application acceptance has not been verified.</p></section>")
    why = evidence['reason'] or 'The available comparison does not establish a specific profile-to-task connection.'
    fit_limit = ' This supports topical fit only.' if evidence['reason'] else ''
    task = evidence['task']
    # Excerpt boundaries are explicit; complete wording stays in the disclosure.
    excerpt = (' '.join(task['text'].split()) if task else '')
    if len(excerpt) > 210:
        excerpt = excerpt[:210].rsplit(' ', 1)[0] + '…'
    source_blocks = ''.join(
        f"<section><h5>{e(b['heading'])}</h5><p class='source-reference'>{e(b['reference'])} · Unassessed source wording</p>"
        f"<blockquote>{e(b['text'])}</blockquote></section>" for b in evidence['blocks'])
    return (
        f"<section class='card-evidence' data-source-variant='{e(evidence['job_id'])}'>"
        f"<p class='opportunity-type'>{e(evidence['kind'])}</p>"
        "<h4>Why this appeared</h4>"
        f"<p>{e(why)}{fit_limit}</p>"
        + (f"<p class='source-task'><strong>Source excerpt:</strong> “{e(excerpt)}”</p>" if excerpt else '')
        + "<h4>What to check before applying</h4>"
        f"<p>{e(evidence['geography'])}</p>"
        + (f"<p class='source-workload'><strong>Source workload wording:</strong> {e(evidence['workload'])}</p>"
           if evidence['workload'] else '')
        + "<p>Source conditions below have not been assessed against your profile.</p>"
        f"<details class='card-source-disclosure'><summary id='{e(card_id)}-source-summary'>Qualifications &amp; source conditions</summary>"
        "<div class='card-source-body'>"
        "<p>Original labels, preferences and alternatives are retained. These quotations are not eligibility verdicts.</p>"
        + (f"<p><strong>Source listing commitment field:</strong> {e(evidence['listing_commitment'])}. "
           "This field and the description wording have not been reconciled or compared to your availability.</p>"
           if evidence['listing_commitment'] else '')
        + source_blocks
        + f"<p>Source record: {e(evidence['external_id'])}. Captured {e(evidence['captured_at'])}.</p>"
        f"<p><a href='{e(evidence['url'])}' rel='noopener noreferrer'>Original source for this variant</a></p>"
        "</div></details><p class='application-uncertainty'>Application acceptance has not been verified. "
        "A catalog observation does not establish an active vacancy.</p></section>"
    )

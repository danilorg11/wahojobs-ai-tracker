"""Source-only presentation for Browse and ordinary details, not matching policy."""
import json
import re

from wahojobs.candidate_source_display import pay_facts, plain


def has_applicant_geography(eligibility):
    return bool(eligibility.get('countries') or eligibility.get('regions')
                or eligibility.get('scope') not in (None, '', 'unknown')
                or eligibility.get('dimension_details'))


def location_summary(job):
    from wahojobs.public_job_page import candidate_job_eligibility, enum_label
    eligibility = candidate_job_eligibility(job)
    parts = [enum_label(eligibility.get('mode'))]
    if has_applicant_geography(eligibility):
        parts.append(eligibility.get('summary'))
    return ' · '.join(part for part in parts if part)


_NON_PAY = re.compile(r'\b(?:referr?als?|referring|refer a|bonus(?:es)?|rewards?|'
                      r'example|e\.g\.|such as|subscription|tuition|expenses?|'
                      r'not paid|no pay|previous salary|expected (?:pay|salary|rate))\b', re.I)


def pay_source_text(text):
    """Exclude unrelated monetary contexts, including their Markdown sections."""
    lines, excluded_section = [], False
    for line in text.splitlines():
        label = plain(line).strip('#: ').casefold()
        heading = bool(re.match(r'^\s*#{1,6}\s|^\s*\*\*[^*]+\*\*:?\s*$', line)) or label in {
            'referrals', 'referral rewards', 'referral bonus', 'bonuses', 'rewards',
            'compensation', 'pay', 'salary', 'responsibilities', 'requirements', 'benefits'}
        if heading:
            excluded_section = bool(_NON_PAY.search(plain(line)))
        if not excluded_section and not _NON_PAY.search(plain(line)):
            lines.append(line)
    return '\n'.join(lines)


def _salary_agrees(comp, wording):
    from wahojobs.candidate_source_display import _CURRENCY, _RATE
    if comp.get('disclosed') is not True:
        return True
    if wording in ('Per accepted task', 'Output-based pay'):
        return True
    rate = _RATE.search(wording)
    if not rate:
        return False
    # Compare the formatter's already-recognized quotation, including task pay,
    # trailing currency and thousands separators. This does not derive fields.
    amounts = re.findall(r'\d+(?:[,.]\d+)*', rate.group())
    def number(value):
        if re.fullmatch(r'\d{1,3}(?:,\d{3})+(?:\.\d+)?', value):
            value = value.replace(',', '')
        return float(value.replace(',', '.'))
    try:
        numbers = [number(value) for value in amounts]
    except ValueError:
        return False
    if len(numbers) not in (1, 2):
        return False
    unit = re.search(r'(?:/\s*|per\s+)(?:accepted\s+)?(hour|hr|month|year|project|task)s?\b', rate.group(), re.I)[1].lower()
    parsed = dict(amount_min=numbers[0], amount_max=numbers[-1], period='hour' if unit == 'hr' else unit)
    if any(comp.get(key) is not None and comp[key] != parsed[key] for key in ('amount_min', 'amount_max')):
        return False
    if comp.get('period') not in (None, 'unknown', parsed['period']):
        return False
    currencies = {c.upper() for c in _CURRENCY.findall(wording)}
    currencies.update(code for symbol, code in (('€','EUR'),('£','GBP')) if symbol in wording)
    return not (comp.get('currency') and currencies and currencies != {comp['currency']})


def advertised_compensation(job):
    """Display accepted exact-source pay without filling unknown semantic fields.

    Manual values/unknowns and scoped conflicts remain authoritative. Literal
    source wording preserves currency symbols and limits the numeric normalizer
    cannot always represent. Nothing is copied between sibling variants.
    """
    from wahojobs.public_job_page import compensation_label
    from wahojobs.crawler.provider_details import DETAIL_KEY
    from wahojobs.opportunity_enrichment import source_body_paragraphs
    comp = job['enrichment']['attributes']['compensation']
    prefix = 'attributes.compensation.'
    overridden = any(path.startswith(prefix) for path in job.get('overridden_fields', []))
    compact = dict(comp, notes=None)
    if overridden:
        return compensation_label(compact)
    if comp.get('disclosed') is False:
        return None
    facts = [f for f in job['enrichment'].get('variant_facts', [])
             if f['field_path'].startswith(prefix)]
    # A deliberately empty or conflicting scoped field is not a missing parse.
    if any(f.get('knowledge_state') == 'known_empty' for f in facts):
        return compensation_label(compact)
    for path in {f['field_path'] for f in facts}:
        values = {json.dumps(f.get('value'), sort_keys=True) for f in facts if f['field_path'] == path}
        if len(values) > 1:
            return None
    bound = (job.get('rich_provider') == job.get('company_slug')
             and job.get('rich_external_id') == job.get('external_id')
             and job.get('rich_source_url') == job.get('listing_url')
             and bool(job.get('rich_external_id')))
    if bound:
        try:
            metadata = json.loads(job.get('rich_metadata_json') or '{}')
        except (TypeError, ValueError):
            metadata = {}
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        detail = metadata.get(DETAIL_KEY)
        if not isinstance(detail, dict) or any(detail.get(k) != job.get(s) for k, s in (
                ('provider', 'company_slug'), ('external_id', 'external_id'), ('url', 'listing_url'))):
            detail = {}
        if job.get('company_slug') == 'alignerr':
            from wahojobs.source_detail_presentation import bound_alignerr_job_detail
            accepted = bound_alignerr_job_detail(job)
            detail = accepted[1] if accepted else {}
        metadata[DETAIL_KEY] = dict(detail)
        record = detail.get('record')
        if isinstance(record, dict):
            metadata[DETAIL_KEY]['record'] = dict(record, shortDescription=pay_source_text(record.get('shortDescription') or ''))
        metadata['pay'] = pay_source_text(metadata.get('pay') or '')
        body = job.get('rich_body') or ''
        if job.get('rich_body_format') == 'text/html':
            # Preserve heading boundaries before the existing HTML-to-text pass.
            body = re.sub(r'<h[1-6]\b[^>]*>', '<p>## ', body, flags=re.I)
            body = re.sub(r'</h[1-6]\s*>', '</p>', body, flags=re.I)
        text = '\n\n'.join(source_body_paragraphs(body, job.get('rich_body_format')))
        pay = pay_facts(metadata, pay_source_text(text))
        if pay['label']:
            if pay['notes'] and pay['label'] not in ('Per accepted task', 'Output-based pay'):
                return None  # Conflicting source rates must not become one settled rate.
            # A literal symbol remains literal; no repeated currency warning.
            return pay['label'].replace(' (currency not specified)', '') if _salary_agrees(comp, pay['label']) else None
        # Do not resurrect a normalized amount extracted from unrelated rewards.
        if _NON_PAY.search(text) and pay_facts({}, text)['label']:
            return None
    return compensation_label(compact)

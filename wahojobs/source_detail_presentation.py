"""Read accepted Alignerr wording separately from formatter-added page fields.

No source rewrite, eligibility inference or HTTP parsing. Historical display_text
is a cached presentation, not authority to append prose to the employer's body.
"""
from html import escape
import json

from wahojobs.source_capture import normalize_source_body


def bound_alignerr_job_detail(job):
    """Adapt a public job row using the same exact listing/source identity."""
    if (job.get('company_slug') != 'alignerr'
            or job.get('rich_provider') != job.get('company_slug')
            or job.get('rich_external_id') != job.get('external_id')
            or job.get('rich_source_url') != job.get('official_url')
            or job.get('rich_source_url') != job.get('listing_url')):
        return None
    source = dict(source_slug=job['company_slug'], external_id=job.get('external_id'),
                  url=job.get('official_url'), body=job.get('rich_body'),
                  body_format=job.get('rich_body_format'))
    try:
        metadata = json.loads(job.get('rich_metadata_json') or '{}')
        detail = bound_alignerr_detail(source, metadata.get('wahojobs_source_detail_v1')) if isinstance(metadata, dict) else None
    except (ValueError, TypeError):
        return None
    return (source, detail) if detail else None


def bound_alignerr_detail(source, detail):
    if not isinstance(detail, dict) or source.get('source_slug') != 'alignerr':
        return None
    if detail.get('version') != 1 or any(detail.get(k) != source.get(s) for k, s in (
            ('provider', 'source_slug'), ('external_id', 'external_id'), ('url', 'url'))):
        return None
    from wahojobs.crawler.provider_details import validate_detail_url
    try:
        validate_detail_url('alignerr', source['external_id'], source['url'])
    except (KeyError, TypeError, ValueError):
        return None
    record = detail.get('record')
    field = detail.get('field')
    formats = {'props.pageProps.job.longDescription': 'text/markdown',
               'props.pageProps.job.htmlLongDescription': 'text/html'}
    if (not isinstance(record, dict) or record.get('id') != source.get('external_id')
            or not isinstance(field, str) or field not in formats
            or source.get('body_format') != formats[field]):
        return None
    from wahojobs.source_capture import parse_source_timestamp, SOURCE_TIMESTAMP_VALID
    if (not isinstance(detail.get('observed_at'), str)
            or parse_source_timestamp(detail['observed_at'])[0] != SOURCE_TIMESTAMP_VALID):
        return None
    body = source.get('body')
    if (not isinstance(body, str) or not body.strip()
            or not isinstance(record.get(field.rsplit('.', 1)[1]), str)
            or normalize_source_body(record.get(field.rsplit('.', 1)[1])) != normalize_source_body(body)):
        return None
    return detail


def bound_applicant_invitation_countries(source, detail):
    """Read existing prepared recruitment evidence, without parsing geography.

    These countries are positively mentioned in an applicant invitation, not an
    exhaustive eligibility list. Generic page location tags are never read here.
    """
    from hashlib import sha256
    from wahojobs.profiles.countries import normalize_country

    detail = bound_alignerr_detail(source, detail)
    if detail is None:
        return ()
    prepared = detail.get('applicant_location_support') or {}
    body = normalize_source_body(source.get('body')) or ''
    if (not isinstance(prepared, dict) or prepared.get('version') != 1
            or prepared.get('body_sha256') != sha256(body.encode()).hexdigest()):
        return ()
    clauses = prepared.get('clauses')
    if not isinstance(clauses, list) or not clauses:
        return ()
    countries = set()
    for clause in clauses:
        if (not isinstance(clause, dict) or clause.get('modality') != 'invitation'
                or clause.get('mode') != 'allow' or clause.get('dimension') != 'location'
                or clause.get('unresolved') is not False or clause.get('source_conflict')
                or clause.get('ambiguous_statement') or not clause.get('source_field')
                or not isinstance(clause.get('source_quote'), str) or not clause['source_quote']
                or clause['source_quote'] not in body
                or not isinstance(clause.get('countries'), list)):
            return ()
        try:
            countries.update(normalize_country(country) for country in clause['countries'])
        except (TypeError, ValueError):
            return ()
    return tuple(sorted(countries))


def alignerr_location_provenance(source, detail):
    detail = bound_alignerr_detail(source, detail)
    if detail is None:
        return None
    value = detail['record'].get('location')
    if not isinstance(value, str) or not value.strip() or value.casefold() == 'remote':
        return None
    from wahojobs.profiles.countries import normalize_country
    try:
        normalize_country(value)
        generic_country_tag = True
    except ValueError:
        generic_country_tag = False
    return dict(value=value, generic_country_tag=generic_country_tag, source_field='props.pageProps.job.location',
                source_url=source['url'], external_id=source['external_id'],
                job_id=source.get('job_id'), material_content_sha256=source.get('material_content_sha256'),
                observed_at=detail.get('observed_at'))


def render_location_provenance(provenance):
    # A generic page country has no established applicant semantics. Keep it in
    # the internal evidence packet, never in candidate HTML (including disclosure
    # and accessibility text). Independent applicant evidence has its own path.
    if not provenance or provenance.get('generic_country_tag') is not False:
        return ''
    # Unrecognized wording may contain an explicit restriction. Preserve that
    # wording conservatively; this view neither interprets nor grants eligibility.
    return '<p><strong>Location information:</strong> ' + escape(provenance['value']) + '</p>'


def alignerr_other_fields(source, detail):
    detail = bound_alignerr_detail(source, detail)
    if detail is None:
        return []
    record = detail['record']
    return [(label, str(record[key])) for key, label in (
        ('jobType', 'Engagement type'), ('lowerBoundHourlyRate', 'Hourly range minimum'),
        ('upperBoundHourlyRate', 'Hourly range maximum'))
        if type(record.get(key)) in (str, int, float)]


def render_other_fields(fields):
    if not fields:
        return ''
    return ('<details class="candidate-published-fields"><summary>Other employer page fields</summary>'
            '<p>These separate page-data fields are shown by Wahojobs alongside the original description.</p><dl>'
            + ''.join('<dt>' + escape(label) + '</dt><dd>' + escape(value) + '</dd>' for label, value in fields)
            + '</dl></details>')

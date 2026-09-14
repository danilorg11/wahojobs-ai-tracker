"""Read accepted Alignerr wording separately from formatter-added page fields.

No source rewrite, eligibility inference or HTTP parsing. Historical display_text
is a cached presentation, not authority to append prose to the employer's body.
"""
from html import escape

from wahojobs.source_capture import normalize_source_body


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

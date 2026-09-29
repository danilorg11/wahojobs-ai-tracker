"""Retained, role-bound destinations and OneForma application-option facts.

Prepared once with the public generation. Never fetch or renew source trust.
"""
import re
from urllib.parse import urlsplit

from wahojobs.catalog_source_presentation import bound_metadata


def oneforma_metadata(job):
    if (job.get('company_slug') != 'oneforma'
            or job.get('rich_source_type') != 'oneforma-wordpress-marketplace'):
        return {}
    metadata = bound_metadata(job)
    if metadata.get('variant_apply_url') == job.get('listing_url'):
        return metadata
    return {}


def application_only(url):
    path = urlsplit(url or '').path.casefold()
    return bool(re.search(r'(?:^|[/_-])(?:signup|sign-up|register|registration|login)(?:$|[/_-])', path))


def prepare_source_links(job):
    from wahojobs.public_job_page import first_human_facing_url
    source = apply = first_human_facing_url(job.get('official_url'))
    oneforma = oneforma_metadata(job)
    if oneforma:
        public = first_human_facing_url(oneforma.get('public_url'))
        parsed = urlsplit(public or '')
        # A captured project permalink, never a guessed slug or homepage.
        if (parsed.scheme == 'https' and parsed.netloc == 'www.oneforma.com'
                and re.fullmatch(r'/(?:projects|jobs)/[^/]+/?', parsed.path)):
            source = public
        elif urlsplit(apply or '').hostname == 'my.oneforma.com':
            source = None
    elif job.get('company_slug') in ('dataforce', 'dataannotation', 'handshake'):
        # These providers retain role-owned, validated application actions.
        metadata = bound_metadata(job)
        apply = first_human_facing_url(metadata.get('application_url')) or apply
    if application_only(source):
        source = None
    return dict(apply=apply, source=source, registration=application_only(apply))


def oneforma_work_facts(job, text):
    """Only explicit work-mode fields and country-bound application options.

    Language locales and translation pairs alone do not establish residence.
    The body must explicitly identify options as applicant countries/markets.
    """
    metadata = oneforma_metadata(job)
    if not metadata:
        return {}
    modes = set(re.findall(r'^Work Mode:\s*(remote|hybrid|on[- ]?site)\s*$', text, re.I | re.M))
    if re.search(r'^Location:\s*Remote, on your own schedule\s*$', text, re.I | re.M):
        modes.add('remote')
    modes = {re.sub(r'[- ]', '', mode.casefold()) for mode in modes}
    result = dict(mode=next(iter(modes))) if len(modes) == 1 else {}
    country_options = re.search(r'\bin the country selected during application\b|'
        r'\bSelect the country and language that match you from the list below\b', text, re.I)
    label = metadata.get('variant_language') or ''
    parts = re.split(r'\s+[-–—]\s+', label)
    if country_options and len(parts) == 2:
        from wahojobs.matching.languages import find_language_mentions
        from wahojobs.matching.locations import countries_in_location
        from wahojobs.profiles.countries import normalize_country
        if find_language_mentions(parts[0]) and not countries_in_location(parts[0]):
            try:
                result['country'] = normalize_country(parts[1])
            except ValueError:
                pass
    return result

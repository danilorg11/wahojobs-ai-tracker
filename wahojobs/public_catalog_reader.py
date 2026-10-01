"""Anonymous, source-only reader prepared by the existing inventory lifecycle.

No database, account, source transport or model is reachable from a page request.
Publication already stops the application under its lifetime lease; restart
prepares the new generation before readiness. Time expiry only removes entries.
"""
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import json
import re
import threading
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape as xml_escape

from wahojobs import public_job_page as detail, public_jobs_catalog as catalog, public_catalog_brand as brand
from wahojobs.authenticated_card_evidence import _source_text
from wahojobs.catalog_source_presentation import (PRESENTATION_VERSION, dataforce_description,
                                                 contribution_context, surge_role_fields, bound_metadata)

PUBLIC_ORIGIN = 'https://www.wahojobs.com'
ORIGIN_PREFIX = '/_catalog'
KEY_HEADER = 'X-Wahojobs-Catalog-Key'
ROBOTS_HEADER = 'X-Wahojobs-Catalog-Robots'
DETAIL_PATH = re.compile(r'/jobs/opportunity-([1-9][0-9]{0,18})\Z')
MAX_VARIANTS = 100_000
MAX_RESPONSE_BYTES = 1_048_576
MAX_CACHE_BYTES = 8 * MAX_RESPONSE_BYTES


@dataclass(frozen=True, slots=True)
class PublicCatalogResponse:
    # Shared anonymous responses have no one-shot account delivery lease.
    # Keep the cached bytes and headers immutable across HTTP deliveries.
    status: int
    body: bytes
    headers: tuple


def publication_quality(job):
    """Small admission check, not an extractor or a new matching policy."""
    title = detail.clean(job.get('source_title'))
    url = detail.first_human_facing_url(job.get('official_url'))
    if not title or not detail.clean(job.get('company_name')) or not url:
        return 'withheld', 'identity_or_destination_missing', ''
    if job.get('company_slug') == 'micro1':
        return 'withheld', 'source_disabled', ''
    if job.get('company_slug') == 'dataforce' and re.search(r'\bviola\b', title, re.I):
        return 'withheld', 'known_title_body_conflict', ''
    body = job.get('rich_body') or ''
    if job.get('official_url') != job.get('listing_url'):
        return 'withheld', 'source_identity_mismatch', ''
    has_source = bool(job.get('has_rich_content') or body or job.get('rich_source_url'))
    if has_source and (job.get('rich_external_id') != job.get('external_id')
            or job.get('rich_source_url') != job.get('listing_url')
            or job.get('rich_provider') != job.get('company_slug')):
        return 'withheld', 'source_identity_mismatch', ''
    if not body.strip():
        return 'limited', 'description_missing', ''
    source = dict(external_id=job.get('external_id'), source_slug=job.get('company_slug'),
                  url=url, body=body, body_format=job.get('rich_body_format'),
                  metadata_json=job.get('rich_metadata_json'))
    try:
        scoped = dataforce_description(job)
        text = scoped if scoped is not None else _source_text(source)
    except (ValueError, TypeError, KeyError):
        return 'withheld', 'invalid_source_document', ''
    if (len(text.encode('utf-8')) > 65_536 or '\x00' in text
            or re.search(r'<(?:script|style)\b|__NEXT_DATA__|webpackChunk|access denied|'
                         r'verify (?:that )?you are human|enable javascript and cookies', text, re.I)):
        return 'withheld', 'unusable_description', ''
    # Retained Romansh registration instructions ask for Italian proficiency.
    # Hold that evidenced contradiction until the source wording is corrected.
    if (job.get('company_slug') == 'welocalize'
            and re.search(r'\btalent pool\s*:\s*romansh speakers\b', title, re.I)
            and re.search(r'\byour italian proficiency\b', text, re.I)):
        return 'withheld', 'known_language_body_conflict', ''
    if (job.get('company_slug') == 'turing' and title.casefold() == 'atlassian jira admin'
            and re.match(r'\s*(?:#{1,6}\s*)?IT Support Specialist\b', text, re.I)):
        return 'withheld', 'known_title_body_conflict', ''
    if (job.get('company_slug') == 'turing' and title.casefold() == 'electrical engineering'
            and re.search(r'We are seeking experienced Aerospace\s*/\s*Flight-Dynamics Engineer\b', text, re.I)
            and re.search(r'experience working in Aerospace\s*/\s*Flight-Dynamics\b', text, re.I)):
        return 'withheld', 'known_title_body_conflict', ''
    if (job.get('company_slug') == 'turing'
            and re.match(r'Agentic Coding Annotator\b', title, re.I)
            and re.search(r'We are looking for an experienced DevOps Engineer to build and operate GPU infrastructure\b', text, re.I)
            and re.search(r'You will own infrastructure across the lifecycle', text, re.I)):
        return 'withheld', 'known_title_body_conflict', ''
    # An explicit document title is an identity assertion, unlike incidental
    # professional-field words in qualifications. Retain unresolved cases for review.
    named = re.search(r'(?im)^\s*(?:#{1,6}\s*)?(?:job title|position title)\s*:\s*(.+)$', text)
    if named:
        normalize = lambda value: re.sub(r'\W+', ' ', value).strip().casefold()
        stated, listed = normalize(named[1]), normalize(title)
        if stated not in listed and listed not in stated:
            # Retained documents use the same role words followed by "AI
            # Trainer"; listing titles add "Freelance" and "Project".
            # Normalize only that suffix, retaining every role qualifier.
            suffix = r'\b(?:freelance )?ai trainer(?: project)?$'
            same_role_alias = (job.get('company_slug') == 'meridial'
                and re.sub(suffix, 'ai trainer', stated) == re.sub(suffix, 'ai trainer', listed))
            if not same_role_alias:
                return 'withheld', 'unresolved_title_variation', ''
    if len(text.strip()) < 200:
        return 'limited', 'short_description', text
    return 'indexable', 'source_description_available', text


def opportunity_label(job, text):
    wording = (job.get('source_title') or '') + '\n' + text
    if re.search(r'\btalent (?:pool|network)\b', job.get('source_title') or '', re.I):
        return 'Talent network — future consideration'
    if re.search(r'\b(?:join|become part of) (?:our |the |an? )?[^\n.!?]{0,60}\btalent (?:pool|network)\b|'
                 r'\b(?:part of (?:this|our|the)|(?:opportunity|position|listing) is for (?:our|the|this|a)) '
                 r'talent (?:pool|network)\b', wording, re.I):
        return 'Talent network — future consideration'
    if (job.get('opportunity_kind') == 'evergreen_application'
            or re.search(r'\bstanding listing\b|\bnot a specific job opening\b', text, re.I)):
        return 'Ongoing application opportunity'
    return ''


def prepare_publication(jobs):
    prepared, decisions = [], []
    count = 0
    for group in jobs:
        variants = []
        for source in group.get('_catalog_variants', (group,)):
            count += 1
            if count > MAX_VARIANTS:
                raise ValueError('public_catalog_too_large')
            state, reason, text = publication_quality(source)
            decisions.append(dict(provider=source['company_slug'], job_id=source['job_id'],
                canonical_id=source['canonical_opportunity_id'], state=state, reason=reason))
            if state == 'withheld':
                continue
            # Do not retain any accidental account decoration on a shared row.
            job = {key: value for key, value in source.items()
                   if not key.startswith(('_authenticated', '_catalog_saved', 'workflow'))}
            job.update(path='/jobs/opportunity-' + str(job['canonical_opportunity_id']),
                       company_path=None, jobposting_evidence=None,
                       _public_state=state, _public_reason=reason, _public_source_text=text,
                       _public_kind=opportunity_label(job, text))
            job.pop('catalog_detail_target', None)
            # Canonical summaries are not the description of a selected variant.
            from wahojobs.catalog_display import advertised_compensation
            # HTML parsing and compensation normalization happen before readiness,
            # never on ordinary navigation or response-cache expiry.
            job.pop('_public_compensation', None)
            job['_public_compensation'] = advertised_compensation(job)
            if job['_public_compensation'] is None:
                from wahojobs.catalog_display import pay_source_text
                from wahojobs.candidate_source_display import pay_facts
                pay_metadata = bound_metadata(job)
                if pay_metadata.get('pay'):
                    source_pay = pay_facts({'pay': pay_source_text(pay_metadata['pay'])}, pay_source_text(text))
                    if source_pay['notes'] and source_pay['label'] not in ('Per accepted task', 'Output-based pay'):
                        decisions[-1].update(state='withheld', reason='conflicting_source_compensation')
                        continue
            activity, quote = contribution_context(job, text, job['_public_compensation'], surge_role_fields(job))
            kind = job['_public_kind']
            if re.search(r'\bone[- ]time\b', job['_public_compensation'] or '', re.I):
                kind = 'One-time participation'
                job['_public_engagement'] = kind
            elif kind in ('Public application opportunity', 'Advertised opportunity'):
                kind = ''
            job['_public_activity'] = activity
            job['_public_kind'] = ' · '.join(filter(None, (activity, kind)))
            job['catalog_summary'] = quote
            from wahojobs.catalog_source_links import prepare_source_links
            job['_public_links'] = prepare_source_links(job)
            from wahojobs.catalog_source_geography import prepare_public_geography
            job['_public_geography'] = prepare_public_geography(job, text)
            catalog.prepare_catalog_location(job, job['_public_geography'])
            variants.append(job)
        if variants:
            representative = dict(max(variants, key=catalog.representative_variant_rank))
            representative['_catalog_variants'] = tuple(variants)
            prepared.append(representative)
    return tuple(prepared), tuple(decisions)


def preparation_metadata(connection):
    """Source inventory only; called once before listening, never by navigation."""
    rows = connection.execute('''SELECT co.id, c.slug AS provider, co.is_active,
        count(j.id) AS variants, sum(j.is_active) AS active_variants
        FROM canonical_opportunities co JOIN companies c ON c.id=co.company_id
        LEFT JOIN jobs j ON j.canonical_opportunity_id=co.id
        WHERE co.canonical_title NOT LIKE '[SIMULATION]%'
        GROUP BY co.id ORDER BY co.id''').fetchall()
    states = {int(r['id']): dict(r) for r in rows}
    return states, dict(
        latest_crawl_run_id=connection.execute('SELECT max(id) FROM crawl_runs').fetchone()[0],
        latest_capture_id=connection.execute('SELECT max(id) FROM job_source_content_captures').fetchone()[0])


class PublicCatalogReader:
    def __init__(self, jobs, *, metadata=None, generation=None, public_origin=PUBLIC_ORIGIN,
                 indexable=False, clock=None, available=lambda: None, candidate_enabled=False):
        if public_origin != PUBLIC_ORIGIN:
            raise ValueError('invalid_public_catalog_origin')
        self.public_origin = public_origin
        self.indexable = indexable is True
        self.candidate_enabled = candidate_enabled is True
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._available = available
        self._prepared, self.decisions = prepare_publication(jobs)
        self.metadata = metadata or {}
        self.generation = generation or {}
        identities = [(j['company_slug'], j['job_id'], j.get('source_hash'),
                       j.get('material_content_sha256'), j.get('latest_successful_source_run_at'))
                      for g in jobs for j in g.get('_catalog_variants', (g,))]
        self.generation = dict(self.generation, presentation_version=PRESENTATION_VERSION, publication_sha256=sha256(
            json.dumps([PRESENTATION_VERSION, identities], separators=(',', ':')).encode()).hexdigest())
        self._lock = threading.RLock()
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._refresh(self._clock())
        # Readiness includes a complete usable catalog response, not a shell.
        for path in ('/jobs', '/jobs/sitemap.xml'):
            if self.handle('GET', path, (('Host', 'www.wahojobs.com'),)).status != 200:
                raise ValueError('public_catalog_not_ready')

    def _refresh(self, now):
        self._jobs = tuple(catalog.refresh_catalog_time(self._prepared, now=now))
        self._by_id = {j['canonical_opportunity_id']: j for j in self._jobs}
        self._known = {j['canonical_opportunity_id']: j for j in self._prepared}
        self._valid_from = now
        self._deadline = catalog.catalog_cache_deadline(self._jobs, now)
        self._cache.clear()
        self._cache_bytes = 0

    def coverage(self):
        """Disjoint current canonical totals; variants remain separately counted.

        load_public_jobs deliberately omits inactive/untrusted variants. Account
        for those omissions from source-only metadata rather than silently
        treating the eligible subset as the entire operational inventory.
        """
        providers = {}
        for provider in sorted({d['provider'] for d in self.decisions} |
                               {r['provider'] for r in self.metadata.values()}):
            rows = [d for d in self.decisions if d['provider'] == provider]
            groups = {}
            for row in rows:
                groups.setdefault(row['canonical_id'], []).append(row)
            counts = Counter()
            for items in groups.values():
                state = ('indexable' if any(r['state'] == 'indexable' for r in items) else
                         'limited' if any(r['state'] == 'limited' for r in items) else 'withheld')
                counts[state] += 1
            current = {k: r for k, r in self.metadata.items()
                       if r['provider'] == provider and r['is_active'] and r['active_variants']}
            untrusted_canonical = sum(k not in groups for k in current)
            untrusted_variants = sum(max(0, r['active_variants'] - len(groups.get(k, ())))
                                     for k, r in current.items())
            counts['withheld'] += untrusted_canonical
            variants = Counter(r['state'] for r in rows)
            variants['withheld'] += untrusted_variants
            reasons = Counter(r['reason'] for r in rows if r['state'] == 'withheld')
            if untrusted_variants:
                reasons['source_disabled' if provider == 'micro1' else 'not_currently_source_verified'] += untrusted_variants
            providers[provider] = dict(
                canonical={state: counts[state] for state in ('indexable', 'limited', 'withheld')},
                variants={state: variants[state] for state in ('indexable', 'limited', 'withheld')},
                withheld_reasons=dict(reasons),
                inactive_canonical=sum(1 for r in self.metadata.values()
                    if r['provider'] == provider and (not r['is_active'] or not r['active_variants'])))
        return dict(generation=self.generation, providers=providers)

    def _response(self, status, body, *, content_type='text/html; charset=utf-8', indexable=False):
        if self.candidate_enabled and content_type.startswith('text/html'):
            from wahojobs.public_candidate_controls import CSS, SCRIPT
            body = body.decode('utf-8') if type(body) is bytes else body
            body = body.replace('</nav>', "<a href='/my-jobs'>My Jobs</a></nav>", 1)
            body = body.replace('</head>', '<style>'+CSS+'</style></head>',1)
            body = body.replace('</body>', SCRIPT+'</body>',1)
        body = body.encode('utf-8') if isinstance(body, str) else body
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError('public_response_too_large')
        robots = 'index,follow' if self.indexable and indexable and status == 200 else 'noindex,follow'
        headers = (('Content-Type', content_type), ('Content-Length', str(len(body))),
            ('Cache-Control', 'no-store'), ('X-Robots-Tag', robots), (ROBOTS_HEADER, robots),
            ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'strict-origin-when-cross-origin'),
            ('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; "
                + brand.ASSET_CSP + "script-src 'self' 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"))
        if status == 503:
            headers += (('Retry-After', '60'),)
        return PublicCatalogResponse(status, body, headers)

    def _message(self, status, title, text):
        return self._response(status, '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex,follow"><title>' + escape(title) + ' | Wahojobs</title>'
            + brand.FAVICON + '<style>' + detail.PUBLIC_JOB_CSS + brand.CSS + '</style>'
            '</head><body>' + brand.HEADER + '<main class="public-message"><h1>' + escape(title) + '</h1><p>' + escape(text) + '</p>'
            '<a href="/jobs">Browse current opportunities</a></main></body></html>')

    def handle(self, method, target, headers=(), body_stream=None):
        if method not in ('GET', 'HEAD'):
            return self._message(405, 'Method not allowed', 'Use GET or HEAD.')
        items = list(headers.items()) if hasattr(headers, 'items') else list(headers)
        if [v for k,v in items if k.lower() == 'host'] != ['www.wahojobs.com']:
            return self._message(400, 'Invalid request', 'The requested host is unavailable.')
        if (not isinstance(target, str) or len(target) > 2048 or not target.startswith('/jobs')
                or re.search(r'[\x00-\x20\x7f\\#]', target)):
            return self._message(404, 'Page not found', 'This page is unavailable.')
        try:
            parsed = urlsplit(target)
        except ValueError:
            return self._message(400, 'Invalid request', 'The requested URL is invalid.')
        if parsed.path != '/jobs' and parsed.path != '/jobs/sitemap.xml' and not DETAIL_PATH.fullmatch(parsed.path):
            return self._message(404, 'Page not found', 'This page is unavailable.')
        try:
            self._available()
        except Exception:
            return self._message(503, 'Catalog temporarily unavailable', 'Please try again shortly.')
        with self._lock:
            now = self._clock()
            if now < self._valid_from:
                return self._message(503, 'Catalog temporarily unavailable', 'Please try again shortly.')
            if now >= self._deadline:
                self._refresh(now)
            if target in self._cache:
                self._cache.move_to_end(target)
                return self._cache[target]
            result = self._page(parsed)
            if result.status == 200:
                while self._cache and (len(self._cache) >= 64 or self._cache_bytes + len(result.body) > MAX_CACHE_BYTES):
                    self._cache_bytes -= len(self._cache.popitem(last=False)[1].body)
                self._cache[target] = result
                self._cache_bytes += len(result.body)
            return result

    def _page(self, parsed):
        navigation = "<nav aria-label='Main'><a href='https://www.wahojobs.com/'>Home</a> · <a href='/jobs'>AI Training Jobs</a> · <a href='https://www.wahojobs.com/blog'>Blog</a></nav>"
        if parsed.path == '/jobs':
            params = catalog.parse_catalog_query(parsed.query)
            if params is None:
                return self._message(400, 'Invalid filters', 'Please use the catalog search controls.')
            page = catalog.build_catalog(self._jobs, params)
            if page['requested_page'] != page['page']:
                return self._message(404, 'Page not found', 'This results page does not exist.')
            html = catalog.render_public_jobs_page(page, public_origin=self.public_origin, navigation=navigation, public_reader=True,
                candidate_controls=self.candidate_enabled)
            return self._response(200, html, indexable=not page['filters'])
        if parsed.path == '/jobs/sitemap.xml':
            if parsed.query:
                return self._message(400, 'Invalid request', 'The sitemap has no query parameters.')
            paths = ['/jobs'] + [j['path'] for j in self._jobs
                if any(v['_public_state'] == 'indexable' for v in j['_catalog_variants'])]
            # Verification/collection times are not significant page edits. Omit
            # optional lastmod until there is a complete page-modification proof.
            xml = '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            xml += ''.join('<url><loc>' + xml_escape(self.public_origin+p) + '</loc></url>' for p in sorted(paths))
            return self._response(200, xml + '</urlset>', content_type='application/xml; charset=utf-8')
        identifier = int(DETAIL_PATH.fullmatch(parsed.path)[1])
        if not catalog.valid_query_encoding(parsed.query):
            return self._message(400, 'Invalid request', 'The detail selection is invalid.')
        try:
            params = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
        except ValueError:
            return self._message(400, 'Invalid request', 'The detail selection is invalid.')
        if set(params) - {'variant', 'return_to'} or any(len(v) != 1 for v in params.values()):
            return self._message(400, 'Invalid request', 'The detail selection is invalid.')
        variant = params.get('variant', [None])[0]
        back = params.get('return_to', [None])[0]
        if ((variant is not None and not re.fullmatch('[1-9][0-9]{0,18}', variant))
                or (back is not None and catalog.validate_catalog_return_target(back) is None)):
            return self._message(400, 'Invalid request', 'The detail selection is invalid.')
        group = self._by_id.get(identifier)
        if group is None:
            if identifier in self._known:
                return self._message(200, 'Availability needs rechecking',
                    'We have not recently verified whether this opportunity is still available. It is not confirmed closed.')
            state = self.metadata.get(identifier)
            if state and (not state['is_active'] or not state['active_variants']):
                return self._message(410, 'Listing no longer available', 'This listing is marked inactive in our records.')
            return self._message(404, 'Opportunity unavailable', 'This opportunity is not in the current public catalog.')
        variants = group['_catalog_variants']
        if variant:
            job = next((v for v in variants if v['job_id'] == int(variant)), None)
        else:
            # The canonical URL always represents its best publishable variant.
            job = max(variants, key=lambda v: (v['_public_state'] == 'indexable', catalog.representative_variant_rank(v)))
        if job is None:
            return self._message(404, 'Opportunity unavailable', 'This version is not currently available.')
        html = detail.render_public_job_page(job, public_origin=self.public_origin,
            navigation=navigation, catalog_return_to=back, public_reader=True)
        if self.candidate_enabled:
            from wahojobs.public_candidate_controls import controls
            context = parsed.path + ('?' + parsed.query if parsed.query else '')
            html = html.replace("<div class='hero-actions'>",controls(job,context)+"<div class='hero-actions'>",1)
        return self._response(200, html, indexable=not parsed.query and job['_public_state'] == 'indexable')

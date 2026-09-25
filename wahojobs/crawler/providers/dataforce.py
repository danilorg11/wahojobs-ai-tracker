import html
import hashlib
from http.client import HTTPException
import re
from dataclasses import replace
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request
from wahojobs.crawler.local_inventory import (open_public as urlopen,
    remaining_http_requests, record_pending_qualification_ids)
from wahojobs.daily_source_policy import observed_dataforce_details, controlled_validation_active

from wahojobs.classification import AVAILABILITY_BASIS_PUBLIC_PAGE, OPPORTUNITY_KIND_LIVE_POSTING
from wahojobs.crawler.types import JobCandidate, RecordPromotionAttestation, BODY_OBSERVATION_PRESENT


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "text/html,application/xhtml+xml",
}
MAX_PAGES = 20
MAX_DETAIL_PAGES = 50
_THYME_APPLICATION_TOKENS = {
    '/project/thyme-freelance-writer-spanish-us': 'aL6jslPVXS',
    '/project/thyme-freelance-writer-korean': 'nUhPxBHO_h',
    '/project/thyme-freelance-writer-chinese': 'oiKoZRdJVb',
    '/project/thyme-freelance-writer-polish': 'Xsluc2L17F',
    '/project/thyme-freelance-writer-afrikaans': '90yLNrzcei',
    '/project/thyme-freelance-writer-turkish': 'w_7NPxgBaJ',
    '/project/thyme-freelance-writer-hindi': '9ki119d56_',
    '/project/thyme-freelance-writer-english-us': 'VYqE5ySEMk',
}
QUALIFIED_DETAIL_PATHS = frozenset(_THYME_APPLICATION_TOKENS)
GENERAL_CONTRACT_ID = 'dataforce_thyme_index_detail_family_v2'
_THYME_IDENTITIES = {
    '/project/thyme-freelance-writer-spanish-us': ('Thyme Freelance Writer - Spanish (US)', 'United States'),
    '/project/thyme-freelance-writer-korean': ('Thyme Freelance Writer - Korean (South Korea)', 'Korea Republic'),
    '/project/thyme-freelance-writer-chinese': ('Thyme Freelance Writer - Simplified Chinese (China)', 'China'),
    '/project/thyme-freelance-writer-polish': ('Thyme Freelance Writer - Polish (Poland)', 'Poland'),
    '/project/thyme-freelance-writer-afrikaans': ('Thyme Freelance Writer - Afrikaans (South Africa)', 'South Africa'),
    '/project/thyme-freelance-writer-turkish': ('Thyme Freelance Writer - Turkish (Turkey)', 'Turkey'),
    '/project/thyme-freelance-writer-hindi': ('Thyme Freelance Writer - Hindi (India)', 'India'),
    '/project/thyme-freelance-writer-english-us': ('Thyme Freelance Writer - English (US)', 'United States'),
}


def fetch_dataforce_jobs(projects_url):
    jobs = []
    seen_external_ids = set()

    for page in range(MAX_PAGES):
        page_url = build_page_url(projects_url, page)
        html_text = fetch_page(page_url)
        validate_inventory_page(html_text)
        has_next = validate_pagination(html_text, page)
        page_jobs = parse_jobs_page(html_text, page_url)
        if not page_jobs:
            if page != 0:
                raise ValueError("DataForce empty page does not prove prior pagination complete.")
            break

        for job in page_jobs:
            if job.external_id in seen_external_ids:
                raise ValueError("DataForce returned a duplicate project identifier.")
            seen_external_ids.add(job.external_id)
            jobs.append(job)
        if not has_next:
            break
    else:
        raise RuntimeError(
            "DataForce pagination reached the safety cap before an empty final page."
        )

    return jobs


def build_page_url(projects_url, page):
    if page == 0:
        return projects_url

    parsed = urlparse(projects_url)
    query = parse_qs(parsed.query)
    query["project_type"] = ["All"]
    query["page"] = [str(page)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def fetch_page(url):
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=45) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        if response.status != 200:
            raise RuntimeError(f"DataForce returned HTTP {response.status}.")
        return response.read().decode(charset, errors="replace")


def collect_index_linked_details(jobs):
    """Verify only exact index-linked detail pages, retaining a partial source."""
    if controlled_validation_active():
        ordered = sorted(jobs, key=lambda job: (
            job.commitment != 'Remote',
            'minor' in job.title.casefold() or 'menor' in job.title.casefold(),
        ))[:MAX_DETAIL_PAGES]
    else:
        remaining = remaining_http_requests()
        slots = min(MAX_DETAIL_PAGES, remaining if remaining is not None else MAX_DETAIL_PAGES)
        ordered = select_daily_detail_pages(jobs, slots, datetime.now(timezone.utc).date().toordinal())
    urls = tuple(job.url for job in ordered)
    qualified = []
    inspection_failures = 0
    verification_failures = 0
    with observed_dataforce_details(urls):
        for job in ordered:
            known = urlparse(job.url).path in QUALIFIED_DETAIL_PATHS
            try:
                page = fetch_page(job.url)
                if any(marker in page.casefold() for marker in
                       ('captcha', 'access denied', '403 forbidden', 'verify you are human')):
                    raise ValueError('DataForce detail access or challenge page.')
                if not known and not inspection_detail_matches_index(job, page):
                    raise ValueError('DataForce inspection detail is unrelated to index.')
                if known:
                    qualified.append(qualify_detail_record(job, page))
                elif supported_thyme_family(job):
                    qualified.append(qualify_detail_record(job, page, general=True))
            except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError,
                    OSError, HTTPException):
                if known:
                    verification_failures += 1
                else:
                    inspection_failures += 1
                continue
    accepted={job.external_id for job in qualified}
    pending={job.external_id for job in jobs if job.external_id not in accepted
        and urlparse(job.url).path not in QUALIFIED_DETAIL_PATHS
        and job.commitment == 'Remote'
        and 'minor' not in job.title.casefold() and 'menor' not in job.title.casefold()}
    pages=sorted({(job.source_metadata or {}).get('index_page_url','')+'|'+
                  (job.source_metadata or {}).get('index_page_sha256','') for job in jobs})
    record_pending_qualification_ids(source='dataforce',identities=pending,
        index_sha256=hashlib.sha256('\n'.join(pages).encode()).hexdigest())
    return qualified, len(urls), inspection_failures, verification_failures


def supported_thyme_family(index_job):
    """Only the evidenced remote Thyme AI-writing family can gain V2 authority."""
    path=urlparse(index_job.url)
    return (path.scheme=='https' and path.netloc=='dataforcecommunity.transperfect.com'
        and re.fullmatch(r'/project/thyme-freelance-writer-[a-z0-9-]+',path.path) is not None
        and not path.query and not path.fragment
        and index_job.external_id=='dataforce::'+path.path.lstrip('/')
        and index_job.title.startswith('Thyme Freelance Writer - ')
        and index_job.commitment=='Remote'
        and index_job.department=='Text' and index_job.expertise=='Text'
        and isinstance(index_job.location,str) and bool(index_job.location.strip())
        and (index_job.source_metadata or {}).get('Country') == index_job.location
        and (index_job.source_metadata or {}).get('Type') == 'Remote'
        and 'minor' not in index_job.title.casefold()
        and 'menor' not in index_job.title.casefold())


def inspection_detail_matches_index(job, detail_html):
    """Count an exploratory detail only when its identity matches the card."""
    canonical = re.search(r'<link\b[^>]*rel="canonical"[^>]*href="([^"]+)"',
                          detail_html, re.I)
    if canonical is None or html.unescape(canonical.group(1)) != job.url:
        return False
    headings = [clean_html_text(value) for value in re.findall(
        r'<h1\b[^>]*>(.*?)</h1>', detail_html, re.I | re.S)]
    return job.title in headings


def select_daily_detail_pages(jobs, slots, day_ordinal):
    """Refresh historical roles and rotate new Thyme and exploratory cards.

    The rotation is deterministic for a UTC day and uses no new queue or
    unseen endpoint. Non-Thyme inspected cards remain pending only.
    """
    if type(slots) is not int or slots < 0 or type(day_ordinal) is not int:
        raise ValueError('DataForce detail rotation bounds are invalid.')
    known = [job for job in jobs if urlparse(job.url).path in QUALIFIED_DETAIL_PATHS]
    backlog = [job for job in jobs if job not in known
        and job.commitment == 'Remote'
        and 'minor' not in job.title.casefold()
        and 'menor' not in job.title.casefold()
        and 'onsite' not in job.title.casefold()
        and 'on site' not in job.title.casefold()]
    reserve=1 if backlog and (slots>1 or slots==1 and day_ordinal%2==1) else 0
    known_slots=min(len(known),slots-reserve)
    selected=([known[(day_ordinal*known_slots+offset)%len(known)] for offset in range(known_slots)]
              if known_slots<len(known) else known[:])
    spaces=slots-len(selected)
    family=[job for job in backlog if supported_thyme_family(job)]
    other=[job for job in backlog if job not in family]
    def add_rotated(pool,count):
        if not pool or count<=0:return
        start=(day_ordinal*count) % len(pool)
        for offset in range(len(pool)):
            job=pool[(start+offset)%len(pool)]
            if job not in selected:
                selected.append(job)
                count-=1
            if count<=0 or len(selected)>=slots:return
    if family and other:
        if spaces==1:
            add_rotated(family if day_ordinal%2==0 else other,1)
        else:
            add_rotated(family,(spaces+1)//2)
            add_rotated(other,spaces//2)
            add_rotated(family,slots-len(selected))
            add_rotated(other,slots-len(selected))
    else:
        add_rotated(family or other,spaces)
    return selected


def detail_role_evidence(index_job, detail_html, *, general=False):
    """Match an indexed Thyme writing role to its public application page."""
    parsed = urlparse(index_job.url)
    if (parsed.scheme != 'https' or parsed.netloc != 'dataforcecommunity.transperfect.com'
            or (not general and parsed.path not in QUALIFIED_DETAIL_PATHS)
            or (general and (parsed.path in QUALIFIED_DETAIL_PATHS
                or not supported_thyme_family(index_job)))
            or parsed.query or parsed.fragment
            or index_job.external_id != 'dataforce::' + parsed.path.lstrip('/')
            or index_job.commitment != 'Remote'
            or (not general and (index_job.title, index_job.location) != _THYME_IDENTITIES.get(parsed.path))
            or index_job.department != 'Text' or index_job.expertise != 'Text'
            or 'minor' in index_job.title.casefold()):
        raise ValueError('DataForce index role is outside the qualified scope.')
    if not re.search(r'<link rel="canonical" href="' + re.escape(index_job.url) + r'"', detail_html):
        raise ValueError('DataForce detail canonical identity disagrees with the index.')
    match = re.search(r'field--name-body.*?</h1>', detail_html, re.I | re.S)
    if match is None:
        raise ValueError('DataForce detail body and heading are missing.')
    headings = re.findall(r'<h1[^>]*>(.*?)</h1>', match.group(0), re.I | re.S)
    if not headings or clean_html_text(headings[-1]) != index_job.title:
        raise ValueError('DataForce detail title disagrees with the index.')
    body = re.sub(r'<style.*?</style>|<script.*?</script>', ' ', detail_html, flags=re.I | re.S)
    body_text = clean_html_text(body)
    if ('machine translation technology' not in body_text.casefold()
            or 'this is a fully remote project' not in body_text.casefold()
            or 'seeking freelance short story writers' not in body_text.casefold()):
        raise ValueError('DataForce remote AI writing work is unproven.')
    links = []
    for href in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>\s*Apply Here\s*</a>', detail_html, re.I | re.S):
        application = ''.join(html.unescape(href).split())
        target = urlparse(application)
        query = parse_qs(target.query, keep_blank_values=True)
        token = query.get('registration-type', [])
        if (target.scheme == 'https' and target.netloc == 'hub.transperfect.com'
                and target.path in ('/', '/registration') and not target.fragment
                and set(query) == {'registration-type'} and len(token) == 1
                and (re.fullmatch(r'[A-Za-z0-9_-]{8,80}',token[0]) if general
                     else token[0] == _THYME_APPLICATION_TOKENS[parsed.path])):
            links.append(application.rstrip('&'))
    if not links or len(set(links)) != 1:
        raise ValueError('DataForce role-bound application action is missing or conflicting.')
    return links[0]


def qualify_detail_record(index_job, detail_html, *, general=False):
    application = detail_role_evidence(index_job, detail_html, general=general)
    metadata = dict(index_job.source_metadata or {})
    required = {'index_card_html', 'index_page_url', 'index_page_sha256'}
    if not required <= set(metadata):
        raise ValueError('DataForce exact index card evidence is missing.')
    metadata['application_url'] = application
    return replace(index_job,
        source_body=detail_html, source_body_format='text/html',
        source_metadata=metadata,
        opportunity_kind=OPPORTUNITY_KIND_LIVE_POSTING,
        availability_basis=AVAILABILITY_BASIS_PUBLIC_PAGE,
        include_in_live_market_estimate=True,
        record_promotion_attestation=RecordPromotionAttestation(
            contract_id=GENERAL_CONTRACT_ID if general else 'dataforce_index_detail_record_v1',
            body_observation=BODY_OBSERVATION_PRESENT,
            authority_evidence={
                'external_id': index_job.external_id,
                'index_page_url': metadata['index_page_url'],
                'index_page_sha256': metadata['index_page_sha256'],
                'index_card_sha256': hashlib.sha256(metadata['index_card_html'].encode()).hexdigest(),
                'detail_url': index_job.url,
                'detail_sha256': hashlib.sha256(
                    detail_html.replace('\r\n', '\n').replace('\r', '\n').strip().encode()).hexdigest(),
                'title': index_job.title,
                'application_url': application,
            },
        ))


def validate_inventory_page(html_text):
    """Only recognizable Drupal project views can declare rows or an empty page."""
    lowered = html_text.lower()
    if any(marker in lowered for marker in ("captcha", "access denied", "forbidden", "challenge")):
        raise ValueError("DataForce access or challenge page is not inventory.")
    if not re.search(r'class=["\'][^"\']*\bview-projects\b', html_text, re.I):
        raise ValueError("DataForce project view marker missing.")
    if '<div class="views-row">' not in html_text and not re.search(
        r'class=["\'][^"\']*\bview-empty\b', html_text, re.I
    ):
        raise ValueError("DataForce page has neither project rows nor explicit empty state.")


def validate_pagination(html_text, page):
    """Use the actual view pager, not a generic 200 or an out-of-range empty page."""
    if not re.search(r'<option value="All" selected="selected">', html_text):
        raise ValueError("DataForce all-project filter is not selected.")
    pager = re.search(r'<ul class="pagination js-pager__items">(.*?)</ul>', html_text, re.S)
    if pager is None:
        if page == 0 and '<div class="view-empty">' in html_text:
            return False
        raise ValueError("DataForce pager is missing.")
    active = re.search(r'<li class="page-item active">\s*<span class="page-link">(\d+)</span>', pager.group(1))
    if active is None or int(active.group(1)) != page + 1:
        raise ValueError("DataForce pager does not match requested page.")
    next_link = re.search(r'<a href="([^"]+)"[^>]*rel="next"', pager.group(1))
    if next_link is None:
        return False
    query = parse_qs(urlparse(html.unescape(next_link.group(1))).query)
    if query not in ({"page": [str(page + 1)]},
                     {"project_type": ["All"], "page": [str(page + 1)]}):
        raise ValueError("DataForce next page left the ordered all-project scope.")
    return True


def parse_jobs_page(html_text, page_url):
    jobs = []
    page_sha256 = hashlib.sha256(html_text.encode('utf-8')).hexdigest()
    for block in html_text.split('<div class="views-row">')[1:]:
        job = parse_job_block(block, page_url)
        if job is None:
            raise ValueError("DataForce returned a project row without required fields.")
        jobs.append(replace(job, source_metadata={**(job.source_metadata or {}),
            'index_card_html': block, 'index_page_url': page_url,
            'index_page_sha256': page_sha256}))
    return jobs


def parse_job_block(block, page_url):
    title = extract_title(block)
    href = extract_project_href(block)
    if not title or not href:
        return None

    fields = extract_fields(block)
    category = normalize_category(fields.get("Category"))
    commitment = clean_value(fields.get("Type"))
    country = clean_value(fields.get("Country"))
    city = clean_value(fields.get("City"))
    location = build_location(country, city, commitment)
    absolute_url = urljoin(page_url, href)

    return JobCandidate(
        external_id=f"dataforce::{urlparse(absolute_url).path.strip('/')}",
        title=title,
        location=location,
        url=absolute_url,
        department=category,
        expertise=category,
        commitment=commitment,
        source_body=clean_html_text(block),
        source_body_format="text/plain" if clean_html_text(block) else None,
        source_metadata=fields or None,
    )


def extract_title(block):
    match = re.search(
        r'<div class="views-field views-field-title">\s*<h2 class="field-content">(.*?)</h2>',
        block,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    return clean_html_text(match.group(1))


def extract_project_href(block):
    match = re.search(
        r'href="(/(?:project|study)/[A-Za-z0-9-]+)"',
        block,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return html.unescape(match.group(1))


def extract_fields(block):
    fields = {}
    for label, value in re.findall(
        r"<strong>\s*(Category|Type|Country|City)\s*</strong>\s*<br>\s*(.*?)</p>",
        block,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        fields[clean_html_text(label)] = clean_html_text(value)
    return fields


def build_location(country, city, commitment):
    if city and country:
        return f"{city}, {country}"
    if country:
        return country
    if commitment and commitment.lower() == "remote":
        return "Remote"
    return "Unknown"


def normalize_category(category):
    value = clean_value(category)
    if not value:
        return "Unknown"

    normalized = value.lower()
    labels = {
        "audio": "Audio",
        "image": "Image",
        "photo": "Photo",
        "text": "Text",
        "video": "Video",
    }
    return labels.get(normalized, value)


def clean_html_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return clean_value(value)


def clean_value(value):
    if value is None:
        return None
    value = html.unescape(str(value)).replace("\xa0", " ")
    value = " ".join(value.split())
    return value or None

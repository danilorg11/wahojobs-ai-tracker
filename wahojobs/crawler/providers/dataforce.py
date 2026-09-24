import html
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request
from wahojobs.crawler.local_inventory import open_public as urlopen
from wahojobs.daily_source_policy import observed_dataforce_details

from wahojobs.crawler.types import JobCandidate


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "text/html,application/xhtml+xml",
}
MAX_PAGES = 20
MAX_DETAIL_PAGES = 50


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
    """Retain bounded exact index-linked pages during controlled observation.

    The transport journal owns raw bytes and metadata. This read-only capture
    does not claim that a 200 detail is a qualified application opportunity.
    """
    ordered = sorted(jobs, key=lambda job: (
        job.commitment != 'Remote',
        'minor' in job.title.casefold() or 'menor' in job.title.casefold(),
    ))[:MAX_DETAIL_PAGES]
    urls = tuple(job.url for job in ordered)
    with observed_dataforce_details(urls):
        for url in urls:
            page = fetch_page(url)
            if any(marker in page.casefold() for marker in
                   ('captcha', 'access denied', '403 forbidden', 'verify you are human')):
                raise ValueError('DataForce detail access or challenge page.')
    return len(urls)


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
    for block in html_text.split('<div class="views-row">')[1:]:
        job = parse_job_block(block, page_url)
        if job is None:
            raise ValueError("DataForce returned a project row without required fields.")
        jobs.append(job)
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

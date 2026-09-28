import html
import hashlib
import re
from http.client import HTTPException
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request
from wahojobs.crawler.local_inventory import open_public as urlopen, record_pending_qualification_ids
from wahojobs.daily_source_policy import observed_surge_details
from wahojobs.crawler.types import RecordPromotionAttestation, BODY_OBSERVATION_PRESENT

from wahojobs.classification import (
    AVAILABILITY_BASIS_PUBLIC_PAGE,
    OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
    OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY,
)
from wahojobs.crawler.types import JobCandidate


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "text/html,application/xhtml+xml",
}
WORKFORCE_PATH_PREFIX = "/workforce/"
FELLOWSHIP_PATH = "/fellowship"


@dataclass(frozen=True)
class SurgePage:
    ok: bool
    text: str
    reason: str
    outcome: str


@dataclass(frozen=True)
class WorkforceRecord:
    slug: str
    url: str
    fields: dict
    index_text: str


def fetch_surge_jobs(base_url):
    base_url = base_url.rstrip("/")
    workforce_url = f"{base_url}/workforce"
    fellowship_url = f"{base_url}{FELLOWSHIP_PATH}"

    workforce_page = fetch_required_page(workforce_url, "workforce index")
    workforce_records = extract_workforce_records(workforce_page.text, workforce_url)
    if not workforce_records or len(workforce_records) > 18:
        raise RuntimeError("Surge crawl failed: no workforce links found.")

    jobs = []
    pending = set()
    with observed_surge_details(tuple(record.url for record in workforce_records)):
        for record in workforce_records:
            try:
                detail_page = fetch_required_page(record.url, f"workforce detail {record.url}")
                jobs.append(parse_workforce_detail(record, detail_page.text,
                                                   index_page_html=workforce_page.text))
            except (OSError, HTTPException, RuntimeError, ValueError, TypeError, KeyError):
                pending.add(f"surge::workforce::{record.slug}")
    record_pending_qualification_ids(source='surge', identities=pending,
        index_sha256=hashlib.sha256(workforce_page.text.encode()).hexdigest())

    # Fellowship has no publication authority. Its independent failure must not
    # discard successful, exact workforce observations.
    try:
        fellowship_page = fetch_required_page(fellowship_url, "fellowship page")
        fellowship_job = parse_fellowship_page(fellowship_url, fellowship_page.text)
    except (OSError, HTTPException, RuntimeError, ValueError, TypeError, KeyError):
        fellowship_job = None
    if fellowship_job is not None:
        jobs.append(fellowship_job)

    return jobs, len(workforce_records), fellowship_job is not None


def fetch_required_page(url, label):
    page = fetch_page(url)
    if page.ok:
        return page
    raise RuntimeError(f"Surge crawl failed: {label} returned {page.reason}.")


def fetch_page(url):
    request = Request(url, headers=REQUEST_HEADERS)
    try:
        with urlopen(request, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            text = response.read().decode(charset, errors="replace")
            if response.status == 200:
                return SurgePage(True, text, "HTTP 200", "success")
            return SurgePage(
                False,
                text,
                f"HTTP {response.status}",
                "network_or_site_error",
            )
    except HTTPError as exc:
        outcome = "not_found" if exc.code == 404 else "network_or_site_error"
        return SurgePage(False, "", f"HTTP {exc.code}", outcome)
    except URLError as exc:
        return SurgePage(False, "", str(exc.reason), "network_or_site_error")
    except TimeoutError:
        return SurgePage(False, "", "timeout", "network_or_site_error")


def extract_workforce_records(html_text, page_url):
    records = extract_workforce_records_from_list_items(html_text, page_url)
    if records:
        return records

    parser = WorkforceLinkParser(page_url)
    parser.feed(html_text)
    return [
        WorkforceRecord(
            slug=slug_from_url(url),
            url=url,
            fields={},
            index_text="",
        )
        for url in parser.urls
    ]


def extract_workforce_records_from_list_items(html_text, page_url):
    matches = list(
        re.finditer(
            r'<div\s+data-slug="([^"]+)"[^>]*role="listitem"',
            html_text,
            flags=re.IGNORECASE,
        )
    )
    records = []
    seen = set()
    for index, match in enumerate(matches):
        slug = html.unescape(match.group(1)).strip("/")
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(html_text)
        block = html_text[start:end]
        href = extract_workforce_href(block, page_url)
        if not href:
            continue
        parsed = urlparse(href)
        if not parsed.path.startswith(WORKFORCE_PATH_PREFIX):
            continue
        if slug in seen:
            continue
        seen.add(slug)
        records.append(
            WorkforceRecord(
                slug=slug,
                url=href,
                fields=extract_data_job_fields(block),
                index_text=clean_html_text(block),
            )
        )
    return records


def extract_workforce_href(block, page_url):
    match = re.search(r'href="([^"]*?/workforce/[^"]+)"', block, flags=re.IGNORECASE)
    if not match:
        return None
    absolute_url = urljoin(page_url, html.unescape(match.group(1)))
    parsed = urlparse(absolute_url)
    if parsed.netloc not in ("surgehq.ai", "www.surgehq.ai"):
        return None
    slug = parsed.path.removeprefix(WORKFORCE_PATH_PREFIX).strip("/")
    if not slug:
        return None
    return f"https://surgehq.ai{WORKFORCE_PATH_PREFIX}{slug}"


class WorkforceLinkParser(HTMLParser):
    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.urls = []
        self.seen = set()

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        attrs = dict(attrs)
        href = attrs.get("href")
        if not href:
            return
        absolute_url = urljoin(self.page_url, html.unescape(href))
        parsed = urlparse(absolute_url)
        if parsed.netloc not in ("surgehq.ai", "www.surgehq.ai"):
            return
        if not parsed.path.startswith(WORKFORCE_PATH_PREFIX):
            return
        slug = parsed.path.removeprefix(WORKFORCE_PATH_PREFIX).strip("/")
        if not slug:
            return
        normalized = f"https://surgehq.ai{WORKFORCE_PATH_PREFIX}{slug}"
        if normalized in self.seen:
            return
        self.seen.add(normalized)
        self.urls.append(normalized)


def parse_workforce_detail(record, html_text, *, index_page_html=None):
    fields = dict(record.fields)
    if not fields:
        fields = extract_data_job_fields(html_text)
    detail_fields = extract_data_job_fields(html_text)
    title = clean_value(detail_fields.get("title"))
    if not title or clean_value(fields.get("title")) not in (None, title):
        raise RuntimeError(f"Surge crawl failed: detail title missing or mismatched for {record.url}.")
    if not has_workforce_application(html_text, record.url, title):
        raise RuntimeError(f"Surge crawl failed: application evidence missing for {record.url}.")
    if not title:
        raise RuntimeError(f"Surge crawl failed: missing title for {record.url}.")
    if (not re.search(r'\bRemote\b', record.index_text, re.I)
            or not re.search(r'\b(?:AI|models?|datasets?|training|train)\b',
                ' '.join(fields.get(key, '') for key in ('the-role', 'might-do')), re.I)):
        raise RuntimeError(f"Surge crawl failed: remote AI work is unproven for {record.url}.")

    text = " ".join(
        value
        for value in (
            record.index_text,
            clean_html_text(html_text),
        )
        if value
    )
    department = infer_department(title, text)
    role_body = '\n\n'.join(value for value in
        (title, fields.get('the-role'), fields.get('looking-for'),
         fields.get('might-do'), fields.get('apply')) if value)
    metadata = dict(fields)
    attestation = None
    if index_page_html is not None:
        if type(index_page_html) is not str:
            raise ValueError('Surge index page evidence must be text.')
        matching = [item for item in extract_workforce_records(
            index_page_html, 'https://surgehq.ai/workforce') if item.slug == record.slug]
        if len(matching) != 1 or matching[0] != record:
            raise ValueError('Surge indexed role evidence disagrees with detail.')
        index_page_sha256 = hashlib.sha256(index_page_html.encode()).hexdigest()
        metadata.update(index_record=dict(slug=record.slug, url=record.url,
            fields=record.fields, index_text=record.index_text),
            index_page_sha256=index_page_sha256, index_page_html=index_page_html,
            detail_page_html=html_text)
        attestation = RecordPromotionAttestation(
            contract_id='surge_remote_workforce_record_v1',
            body_observation=BODY_OBSERVATION_PRESENT,
            authority_evidence=dict(external_id=f"surge::workforce::{record.slug}",
                url=record.url, title=title, index_page_sha256=index_page_sha256,
                detail_page_sha256=hashlib.sha256(html_text.encode()).hexdigest(),
                application_email='talent@surgehq.ai'))
    return JobCandidate(
        external_id=f"surge::workforce::{record.slug}",
        title=title,
        location="Remote",
        url=record.url,
        department=department,
        expertise=department,
        commitment=clean_value(fields.get("pay-rate")),
        opportunity_kind=OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY,
        availability_basis=AVAILABILITY_BASIS_PUBLIC_PAGE,
        include_in_live_market_estimate=False,
        source_body=role_body,
        source_body_format="text/plain" if text else None,
        source_metadata=metadata,
        record_promotion_attestation=attestation,
    )


def has_workforce_application(html_text, url, title):
    # Require a role-level call to action; ordinary site navigation is insufficient.
    slug = slug_from_url(url).lower()
    canonical_tags = [tag for tag in re.findall(r'<link\b[^>]*>', html_text, re.I)
                      if re.search(r'\brel="canonical"', tag, re.I)]
    if len(canonical_tags) != 1:
        return False
    canonical = re.search(r'\bhref="([^"]+)"', canonical_tags[0], re.I)
    if canonical is None or html.unescape(canonical.group(1)) != url:
        return False
    for href, label in re.findall(
        r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        html_text, re.I | re.S
    ):
        if "apply" not in normalize_text(clean_html_text(label)):
            continue
        absolute = urljoin(url, html.unescape(href))
        parsed = urlparse(absolute)
        if parsed.scheme == "mailto" and parsed.path and slug in parsed.path.lower():
            return True
        if (parsed.scheme == "https" and parsed.netloc in ("surgehq.ai", "www.surgehq.ai")
                and ("/apply" in parsed.path.lower() or "application" in parsed.path.lower())
                and slug in parsed.path.lower()):
            return True
    # The public page also offers a role-specific email action. The address is
    # shared, but the named role's own application instructions require its
    # title in the message. Bind that action to the exact indexed slug and
    # title; a general footer email or site signup is insufficient.
    match = re.search(
        r'<div\s+data-slug="' + re.escape(slug) +
        r'"\s+class="workforce-popup"', html_text, re.I,
    )
    if match is None:
        return False
    next_role = re.search(r'<div\s+data-slug="[^"]+"[^>]*role="listitem"',
                          html_text[match.end():], re.I)
    if next_role is None:
        return False
    block = html_text[match.start():match.end() + next_role.start()]
    fields = extract_data_job_fields(block)
    if clean_value(fields.get("title")) != title:
        return False
    instructions = normalize_text(fields.get("apply"))
    return ("name of the role" in instructions
            and re.search(r'href="mailto:talent@surgehq\.ai"', block, re.I) is not None)


def parse_fellowship_page(url, html_text):
    text = clean_html_text(html_text)
    normalized = normalize_text(text)
    if "researchfellows@surgehq.ai" not in normalized or "mailto:researchfellows@surgehq.ai" not in html_text.lower():
        raise RuntimeError("Surge crawl failed: fellowship page missing apply email.")
    heading = re.search(r"<h1\b[^>]*>(.*?)</h1>", html_text, re.I | re.S)
    title = clean_html_text(heading.group(1)) if heading else ""
    if "research fellowship" not in normalize_text(title):
        raise RuntimeError("Surge crawl failed: fellowship role title missing.")
    return JobCandidate(
        external_id="surge::fellowship::research-fellowship",
        title=title,
        location=infer_location(text),
        url=url,
        department="STEM Experts",
        expertise="STEM Experts",
        commitment=infer_fellowship_commitment(text),
        opportunity_kind=OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
        availability_basis=AVAILABILITY_BASIS_PUBLIC_PAGE,
        include_in_live_market_estimate=False,
        source_body=text or None,
        source_body_format="text/plain" if text else None,
    )


def extract_data_job_fields(html_text):
    parser = DataJobParser()
    parser.feed(html_text)
    return parser.fields


class DataJobParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = {}
        self.active_field = None
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        data_job = attrs.get("data-job")
        if data_job and self.active_field is None:
            self.active_field = data_job
            self.depth = 1
            self.parts = []
        elif self.active_field is not None:
            self.depth += 1

    def handle_endtag(self, tag):
        if self.active_field is None:
            return
        self.depth -= 1
        if self.depth <= 0:
            value = clean_value(" ".join(self.parts))
            if value and self.active_field not in self.fields:
                self.fields[self.active_field] = value
            self.active_field = None
            self.parts = []

    def handle_data(self, data):
        if self.active_field is not None:
            self.parts.append(data)


def infer_department(title, text):
    title_normalized = normalize_text(title)
    text_normalized = normalize_text(text)
    rules = (
        (("medical", "clinical", "physician", "diagnostic"), "Medical Experts"),
        (("law", "lawyer", "legal", "court", "doj"), "Legal Experts"),
        (("journalist", "author", "writing", "narrative"), "Creative / Media"),
        (("venture capital", "vc partner", "investment"), "Finance Experts"),
        (("investment banker", "m&a", "capital market"), "Finance Experts"),
        (("startup ceo", "chief executive", "executive"), "Business Experts"),
        (("management consultant", "consulting", "mckinsey", "bain", "bcg"), "Business Experts"),
        (("professor", "academic", "research", "stem"), "STEM Experts"),
    )
    for keywords, label in rules:
        if any(keyword in title_normalized for keyword in keywords):
            return label
    for keywords, label in rules:
        if any(keyword in text_normalized for keyword in keywords):
            return label
    return "Expert Network"


def infer_location(text):
    return "Remote" if "remote" in normalize_text(text) else "Unknown"


def infer_fellowship_commitment(text):
    normalized = normalize_text(text)
    parts = []
    if "hourly" in normalized:
        parts.append("hourly compensation")
    if "flexibility" in normalized or "set their own schedules" in normalized:
        parts.append("flexible schedule")
    return "; ".join(parts) if parts else None


def title_from_slug(url):
    slug = slug_from_url(url)
    words = [word for word in slug.split("-") if word]
    return " ".join(word.capitalize() for word in words)


def slug_from_url(url):
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1]


def clean_html_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return clean_value(value) or ""


def clean_value(value):
    if value is None:
        return None
    value = html.unescape(str(value)).replace("\xa0", " ")
    value = value.replace("\u200d", " ")
    value = " ".join(value.split())
    return value or None


def normalize_text(value):
    return re.sub(r"\s+", " ", (value or "").strip().lower())

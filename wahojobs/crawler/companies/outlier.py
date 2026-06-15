import json
import re
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from wahojobs.crawler.types import CompanyCrawlResult, JobCandidate


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1; +https://example.com)",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
}


SAMPLE_JOBS = [
    JobCandidate(
        title="AI Trainer - Coding Expertise",
        location="Remote",
        url="https://outlier.ai/sample/ai-trainer-coding",
        external_id="sample-outlier-coding",
    ),
    JobCandidate(
        title="AI Trainer - Mathematics",
        location="Remote",
        url="https://outlier.ai/sample/ai-trainer-math",
        external_id="sample-outlier-math",
    ),
    JobCandidate(
        title="AI Writing Evaluator",
        location="Remote",
        url="https://outlier.ai/sample/ai-writing-evaluator",
        external_id="sample-outlier-writing",
    ),
]


class LinkTextParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.links = []
        self._current_href = None
        self._text_parts = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href")
        if href:
            self._current_href = urljoin(self.base_url, href)
            self._text_parts = []

    def handle_data(self, data):
        if self._current_href:
            self._text_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._current_href:
            text = " ".join(part.strip() for part in self._text_parts if part.strip())
            self.links.append((text, self._current_href))
            self._current_href = None
            self._text_parts = []


def crawl_outlier(careers_url):
    errors = []
    urls_to_try = [
        careers_url,
        "https://outlier.ai/opportunities",
        "https://app.outlier.ai/en/expert/opportunities",
    ]

    for url in dict.fromkeys(urls_to_try):
        try:
            raw = fetch_url(url)
            jobs = parse_jobs(raw, url)
            if jobs:
                return CompanyCrawlResult(
                    jobs=jobs,
                    used_sample_data=False,
                    source_message=f"Fetched live Outlier jobs from {url}.",
                )
            errors.append(f"{url}: fetched but no job links were recognized")
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            errors.append(f"{url}: {exc}")

    return CompanyCrawlResult(
        jobs=SAMPLE_JOBS,
        used_sample_data=True,
        source_message=(
            "Using SAMPLE DATA because live Outlier jobs could not be fetched or parsed. "
            + " | ".join(errors)
        ),
    )


def fetch_url(url):
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=20) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def parse_jobs(raw, base_url):
    jobs = parse_json_jobs(raw, base_url)
    if jobs:
        return jobs
    return parse_html_jobs(raw, base_url)


def parse_json_jobs(raw, base_url):
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    found = []
    walk_json(data, found)
    return candidates_from_dicts(found, base_url)


def walk_json(value, found):
    if isinstance(value, dict):
        title = value.get("title") or value.get("name")
        url = value.get("url") or value.get("apply_url") or value.get("absolute_url")
        if title and url:
            found.append(value)
        for child in value.values():
            walk_json(child, found)
    elif isinstance(value, list):
        for child in value:
            walk_json(child, found)


def candidates_from_dicts(items, base_url):
    candidates = []
    seen_urls = set()
    for item in items:
        title = clean_text(item.get("title") or item.get("name") or "")
        url = urljoin(base_url, item.get("url") or item.get("apply_url") or item.get("absolute_url") or "")
        location = clean_text(extract_location(item))
        external_id = item.get("id") or item.get("job_id") or item.get("gh_jid")

        if not title or not url or url in seen_urls:
            continue
        seen_urls.add(url)
        candidates.append(
            JobCandidate(
                title=title,
                location=location or "Remote",
                url=url,
                external_id=str(external_id) if external_id else None,
            )
        )
    return candidates


def extract_location(item):
    location = item.get("location")
    if isinstance(location, dict):
        return location.get("name") or location.get("display_name") or ""
    if isinstance(location, str):
        return location
    offices = item.get("offices")
    if isinstance(offices, list) and offices:
        names = [office.get("name") for office in offices if isinstance(office, dict)]
        return ", ".join(name for name in names if name)
    return ""


def parse_html_jobs(raw, base_url):
    parser = LinkTextParser(base_url)
    parser.feed(raw)

    candidates = []
    seen_urls = set()
    for text, href in parser.links:
        if href in seen_urls:
            continue
        if not looks_like_job_link(text, href):
            continue
        seen_urls.add(href)
        candidates.append(
            JobCandidate(
                title=clean_text(text),
                location="Remote",
                url=href,
                external_id=extract_id_from_url(href),
            )
        )
    return candidates


def looks_like_job_link(text, href):
    if not text or len(text.strip()) < 5:
        return False
    combined = f"{text} {href}".lower()
    job_words = [
        "trainer",
        "evaluator",
        "writer",
        "coding",
        "math",
        "language",
        "expert",
        "opportunit",
        "apply",
        "job",
    ]
    return any(word in combined for word in job_words)


def extract_id_from_url(url):
    match = re.search(r"(\d{5,}|[a-f0-9]{16,})", url)
    return match.group(1) if match else None


def clean_text(value):
    return re.sub(r"\s+", " ", str(value)).strip()


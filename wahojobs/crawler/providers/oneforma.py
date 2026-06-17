import html
import json
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from wahojobs.crawler.types import JobCandidate


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json",
    "Referer": "https://www.oneforma.com/jobs/",
}

PAYMENT_TAG_PREFIXES = (
    "fixed rate",
    "hourly",
    "per word",
    "per approved",
)

AUDIO_COLLECTION_KEYWORDS = (
    "audio recording",
    "native speaker",
    "speech model",
    "speech models",
    "recorded audio",
    "voice",
    "audio discussion",
    "speaking naturally",
    "natural speech",
    "recording study",
)


def fetch_oneforma_jobs(api_url):
    posts = fetch_all_posts(api_url)
    jobs = []
    for post in posts:
        jobs.extend(parse_oneforma_post(post))
    return jobs


def fetch_all_posts(api_url):
    first_page, total_pages = fetch_page(api_url, 1)
    posts = list(first_page)
    for page in range(2, total_pages + 1):
        page_posts, _ = fetch_page(api_url, page)
        posts.extend(page_posts)
    return posts


def fetch_page(api_url, page):
    request = Request(add_query_params(api_url, {"page": page}), headers=REQUEST_HEADERS)
    with urlopen(request, timeout=60) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")
        total_pages = int(response.headers.get("X-WP-TotalPages") or "1")

    data = json.loads(payload)
    if not isinstance(data, list):
        raise ValueError("OneForma response was not a job list.")
    return data, total_pages


def add_query_params(url, params):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key, value in params.items():
        query[key] = [str(value)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def parse_oneforma_post(post):
    post_id = post.get("id")
    title = clean_html_text((post.get("title") or {}).get("rendered"))
    if not post_id or not title:
        return []

    public_url = clean_value(post.get("link")) or f"https://www.oneforma.com/?p={post_id}"
    job_types, job_tags = extract_terms(post)
    department = infer_department(post, title, job_types)
    commitment = extract_commitment(job_tags)
    location = extract_location(job_tags)
    apply_rows = extract_apply_rows(post)

    if not apply_rows:
        apply_rows = [{"language": None, "apply_url": None}]

    return build_variants(
        post_id=post_id,
        title=title,
        public_url=public_url,
        department=department,
        commitment=commitment,
        location=location,
        apply_rows=apply_rows,
    )


def infer_department(post, title, job_types):
    if job_types:
        return "; ".join(job_types)

    text = " ".join(
        value
        for value in (
            title,
            clean_html_text((post.get("excerpt") or {}).get("rendered")),
            clean_html_text((post.get("content") or {}).get("rendered")),
        )
        if value
    )
    if has_audio_collection_signal(text):
        return "Data Collection"
    return "Unknown"


def has_audio_collection_signal(text):
    normalized = normalize_text(text)
    return any(keyword in normalized for keyword in AUDIO_COLLECTION_KEYWORDS)


def build_variants(post_id, title, public_url, department, commitment, location, apply_rows):
    parsed_rows = []
    for index, row in enumerate(apply_rows, start=1):
        language = clean_value(row.get("language"))
        apply_url = clean_value(row.get("apply_url"))
        parsed_rows.append(
            {
                "index": index,
                "language": language,
                "apply_url": apply_url,
                "apply_id": parse_apply_id(apply_url),
            }
        )

    apply_id_counts = {}
    for row in parsed_rows:
        if row["apply_id"]:
            apply_id_counts[row["apply_id"]] = apply_id_counts.get(row["apply_id"], 0) + 1

    used_suffixes = set()
    jobs = []
    for row in parsed_rows:
        suffix = choose_variant_suffix(row, apply_id_counts)
        if suffix in used_suffixes:
            suffix = f"{suffix}-{row['index']}"
        used_suffixes.add(suffix)

        variant_title = title
        if row["language"] and len(parsed_rows) > 1:
            variant_title = f"{title} - {row['language']}"

        jobs.append(
            JobCandidate(
                external_id=f"oneforma::{post_id}::{suffix}",
                title=variant_title,
                location=location,
                url=row["apply_url"] or public_url,
                department=department,
                expertise=department,
                commitment=commitment,
            )
        )
    return jobs


def choose_variant_suffix(row, apply_id_counts):
    if row["apply_id"] and apply_id_counts.get(row["apply_id"]) == 1:
        return row["apply_id"]
    if row["language"]:
        return normalize_key(row["language"])
    return f"variant-{row['index']}"


def extract_apply_rows(post):
    acf = post.get("acf") or {}
    rows = acf.get("apply_job") or []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def parse_apply_id(url):
    if not url:
        return None

    query = parse_qs(urlparse(url).query)
    if query.get("jobId"):
        return clean_value(query["jobId"][0])

    match = re.search(r"/jobs/(\d+)", url)
    if match:
        return match.group(1)
    return None


def extract_terms(post):
    embedded = post.get("_embedded") or {}
    term_groups = embedded.get("wp:term") or []
    job_types = []
    job_tags = []

    for group in term_groups:
        if not isinstance(group, list):
            continue
        for term in group:
            if not isinstance(term, dict):
                continue
            name = clean_value(term.get("name"))
            if not name:
                continue
            if term.get("taxonomy") == "job_type":
                job_types.append(name)
            elif term.get("taxonomy") == "job_tag":
                job_tags.append(name)

    return job_types, job_tags


def extract_commitment(job_tags):
    payment_tags = [
        tag
        for tag in job_tags
        if normalize_text(tag).startswith(PAYMENT_TAG_PREFIXES)
    ]
    return "; ".join(payment_tags) if payment_tags else None


def extract_location(job_tags):
    location_tags = [
        tag
        for tag in job_tags
        if not normalize_text(tag).startswith(PAYMENT_TAG_PREFIXES)
    ]
    if location_tags:
        return "; ".join(location_tags)
    return "Worldwide"


def clean_html_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return clean_value(value)


def clean_value(value):
    if value is None:
        return None
    value = html.unescape(str(value))
    value = " ".join(value.split())
    return value or None


def normalize_key(value):
    value = normalize_text(value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unknown"


def normalize_text(value):
    return re.sub(r"\s+", " ", (value or "").strip().lower())

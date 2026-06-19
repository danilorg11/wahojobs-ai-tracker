import json
import re
import struct
from html import unescape
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from wahojobs.classification import (
    AVAILABILITY_BASIS_PUBLIC_CMS,
    OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY,
)
from wahojobs.crawler.types import JobCandidate


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "text/html,application/xhtml+xml,application/json",
}
DETAIL_URL_PREFIX = "https://joinhandshake.com/ai/opportunities"

OPPORTUNITIES_COLLECTION = "Opportunities"
SUBJECT_FILTERS_COLLECTION = "Subject Filters"
DEGREE_FILTERS_COLLECTION = "Degree Filters"

# Framer exposes generated field IDs in the public module metadata. Keep those
# assumptions isolated here so a future CMS rename only touches this provider.
FIELD_ID = "id"
FIELD_TITLE = "Hi7WvygoG"
FIELD_SLUG = "Vt3dK5Eel"
FIELD_SHOW_JOB = "hwOeBJbXG"
FIELD_SALARY = "WFQUwsB06"
FIELD_SUBJECT_FILTERS = "qlLuhSKj_"
FIELD_DEGREE_FILTERS = "L64oFuWs0"

SUBJECT_TITLE_FIELD = "E5JNxEx4j"
DEGREE_TITLE_FIELD = "q0zDwflPE"


def fetch_handshake_jobs(opportunities_url):
    html_text = fetch_text(ensure_trailing_slash(opportunities_url))
    cms_urls = discover_cms_urls(html_text)

    opportunity_records = read_framercms_records(
        cms_urls[OPPORTUNITIES_COLLECTION]
    )
    subject_labels = read_label_map(
        cms_urls.get(SUBJECT_FILTERS_COLLECTION),
        SUBJECT_TITLE_FIELD,
    )
    degree_labels = read_label_map(
        cms_urls.get(DEGREE_FILTERS_COLLECTION),
        DEGREE_TITLE_FIELD,
    )

    return [
        parse_opportunity_record(record, subject_labels, degree_labels)
        for record in opportunity_records
        if should_include_record(record)
    ]


def ensure_trailing_slash(url):
    return url if url.endswith("/") else f"{url}/"


def fetch_text(url):
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=60) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_bytes(url):
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=60) as response:
        return response.read()


def discover_cms_urls(html_text):
    urls = extract_framer_module_urls(html_text)
    cms_urls = {}

    for url in urls:
        module_text = fetch_text(url)
        for collection_name in (
            OPPORTUNITIES_COLLECTION,
            SUBJECT_FILTERS_COLLECTION,
            DEGREE_FILTERS_COLLECTION,
        ):
            if collection_name in cms_urls:
                continue
            if f"displayName:`{collection_name}`" not in module_text:
                continue

            cms_url = extract_collection_chunk_url(module_text)
            if cms_url:
                cms_urls[collection_name] = cms_url

    if OPPORTUNITIES_COLLECTION not in cms_urls:
        raise ValueError("Could not find Handshake Opportunities Framer CMS chunk.")
    return cms_urls


def extract_framer_module_urls(html_text):
    urls = []
    patterns = (
        r'href="(https://framerusercontent\.com/sites/[^"]+\.mjs)"',
        r'src="(https://framerusercontent\.com/sites/[^"]+\.mjs)"',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, html_text):
            url = unescape(match.group(1))
            if url not in urls:
                urls.append(url)
    return urls


def extract_collection_chunk_url(module_text):
    match = re.search(
        r"new URL\(`\./([^`]+-chunk-default-0\.framercms)`,`([^`]+)`\)"
        r"\.href\.replace\(`/modules/`,`/cms/`\)",
        module_text,
    )
    if not match:
        return None

    chunk_name, module_url = match.groups()
    return urljoin(module_url, chunk_name).replace("/modules/", "/cms/")


def read_label_map(cms_url, title_field):
    if not cms_url:
        return {}
    return {
        record.get(FIELD_ID): clean_value(record.get(title_field))
        for record in read_framercms_records(cms_url)
        if clean_value(record.get(FIELD_ID)) and clean_value(record.get(title_field))
    }


def read_framercms_records(cms_url):
    decoder = FramerCmsDecoder(fetch_bytes(cms_url))
    return decoder.read_records()


def should_include_record(record):
    return (
        record.get(FIELD_SHOW_JOB) is True
        and bool(clean_value(record.get(FIELD_TITLE)))
        and bool(clean_value(record.get(FIELD_SLUG)))
        and bool(clean_value(record.get(FIELD_ID)))
    )


def parse_opportunity_record(record, subject_labels, degree_labels):
    record_id = clean_value(record.get(FIELD_ID))
    title = clean_value(record.get(FIELD_TITLE))
    slug = clean_value(record.get(FIELD_SLUG))
    subjects = resolve_labels(record.get(FIELD_SUBJECT_FILTERS), subject_labels)
    degrees = resolve_labels(record.get(FIELD_DEGREE_FILTERS), degree_labels)
    expertise = "; ".join(subjects) if subjects else "Unknown"

    return JobCandidate(
        external_id=f"handshake::{record_id}",
        title=title,
        location="Unknown",
        url=f"{DETAIL_URL_PREFIX}/{slug}",
        department=expertise,
        expertise=expertise,
        commitment=build_commitment(record.get(FIELD_SALARY), degrees),
        opportunity_kind=OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY,
        availability_basis=AVAILABILITY_BASIS_PUBLIC_CMS,
        include_in_live_market_estimate=False,
    )


def resolve_labels(ids, labels):
    if not ids:
        return []
    return [
        labels[item_id]
        for item_id in ids
        if item_id in labels and labels[item_id]
    ]


def build_commitment(salary, degrees):
    parts = []
    if isinstance(salary, (int, float)) and salary > 0:
        parts.append(f"Rate: {format_rate(salary)}/hr")
    if degrees:
        parts.append(f"Degree filters: {', '.join(degrees)}")
    return "; ".join(parts) or None


def format_rate(value):
    if float(value).is_integer():
        return f"${int(value)}"
    return f"${value:.2f}"


def clean_value(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None


class FramerCmsDecoder:
    TYPE_ARRAY = 1
    TYPE_BOOLEAN = 2
    TYPE_DATE = 4
    TYPE_ENUM = 5
    TYPE_LINK = 7
    TYPE_NUMBER = 8
    TYPE_RICH_TEXT = 11
    TYPE_STRING = 12

    def __init__(self, data):
        self.data = data
        self.offset = 0

    def read_records(self):
        records = []
        record_count = self.read_uint32()
        for _ in range(record_count):
            records.append(self.read_record())
        return records

    def read_record(self):
        record = {}
        field_count = self.read_uint16()
        for _ in range(field_count):
            field_name = self.read_string()
            value_type = self.read_byte()
            record[field_name] = self.read_value(value_type)
        return record

    def read_value(self, value_type):
        if value_type in (
            self.TYPE_ENUM,
            self.TYPE_LINK,
            self.TYPE_STRING,
        ):
            value = self.read_string()
            if value_type == self.TYPE_LINK:
                return self.clean_link(value)
            return value
        if value_type == self.TYPE_DATE:
            return self.read_int64()
        if value_type == self.TYPE_NUMBER:
            return self.read_float64()
        if value_type == self.TYPE_BOOLEAN:
            return bool(self.read_byte())
        if value_type == self.TYPE_ARRAY:
            return self.read_array()
        if value_type == self.TYPE_RICH_TEXT:
            return self.read_rich_text()

        raise ValueError(f"Unsupported Framer CMS value type: {value_type}")

    def read_array(self):
        values = []
        item_count = self.read_uint16()
        for _ in range(item_count):
            values.append(self.read_value(self.read_byte()))
        return values

    def read_rich_text(self):
        has_value = self.read_byte()
        if not has_value:
            return None
        return self.read_string()

    def read_string(self):
        length = self.read_uint32()
        value = self.data[self.offset : self.offset + length]
        self.offset += length
        return value.decode("utf-8", errors="replace")

    def read_byte(self):
        value = self.data[self.offset]
        self.offset += 1
        return value

    def read_uint16(self):
        value = struct.unpack_from(">H", self.data, self.offset)[0]
        self.offset += 2
        return value

    def read_uint32(self):
        value = struct.unpack_from(">I", self.data, self.offset)[0]
        self.offset += 4
        return value

    def read_int64(self):
        value = struct.unpack_from(">q", self.data, self.offset)[0]
        self.offset += 8
        return value

    def read_float64(self):
        value = struct.unpack_from(">d", self.data, self.offset)[0]
        self.offset += 8
        return value

    def clean_link(self, value):
        if not value:
            return None
        if value.startswith('"') and value.endswith('"'):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value.strip('"')
        return value

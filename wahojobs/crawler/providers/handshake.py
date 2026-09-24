import json
import hashlib
import re
import struct
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from wahojobs.crawler.local_inventory import open_catalog
from wahojobs.daily_source_policy import current_source, observed_handshake_assets

from wahojobs.classification import (
    AVAILABILITY_BASIS_PUBLIC_CMS,
    OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY,
)
from wahojobs.crawler.types import BODY_OBSERVATION_PRESENT, JobCandidate, RecordPromotionAttestation
from wahojobs.matching.locations import countries_in_location, regions_in_location


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "text/html,application/xhtml+xml,application/json",
}
DETAIL_URL_PREFIX = "https://joinhandshake.com/ai/opportunities"
MAX_MODULES = 32
MAX_CHUNKS_PER_COLLECTION = 8

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
FIELD_WORK_LOCATION = "Sw8QukdCp"
FIELD_APPLICATION = "YP6SW58rm"
FIELD_DESCRIPTION = "y2vadTbOm"

SUBJECT_TITLE_FIELD = "E5JNxEx4j"
DEGREE_TITLE_FIELD = "q0zDwflPE"


class UnresolvedHandshakeFacet(ValueError):
    pass


def fetch_handshake_jobs(opportunities_url):
    html_text = fetch_text(ensure_trailing_slash(opportunities_url))
    modules = extract_framer_module_urls(html_text)
    with observed_handshake_assets(modules):
        cms_urls = discover_cms_urls(html_text)
    chunks = [url for urls in cms_urls.values() for url in urls]

    with observed_handshake_assets(chunks):
        opportunity_rows = read_framercms_rows(
            cms_urls[OPPORTUNITIES_COLLECTION]
        )
        opportunity_records = [record for record, _, _ in opportunity_rows]
        try:
            subject_labels, subject_label_evidence = read_label_map(
                cms_urls.get(SUBJECT_FILTERS_COLLECTION), SUBJECT_TITLE_FIELD)
        except (OSError, ValueError, IndexError, struct.error, UnicodeError):
            subject_labels, subject_label_evidence = {}, {}
        try:
            degree_labels, degree_label_evidence = read_label_map(
                cms_urls.get(DEGREE_FILTERS_COLLECTION), DEGREE_TITLE_FIELD)
        except (OSError, ValueError, IndexError, struct.error, UnicodeError):
            degree_labels, degree_label_evidence = {}, {}

    visible = [record for record in opportunity_records if record.get(FIELD_SHOW_JOB) is True]
    structurally_valid = [record for record in visible if should_include_record(record)]
    # Even a hidden or out-of-scope record can make a promoted identity ambiguous.
    for field in (FIELD_ID, FIELD_SLUG):
        values = [clean_value(record.get(field)) for record in opportunity_records]
        values = [value for value in values if value]
        if len(values) != len(set(values)):
            raise ValueError("Handshake public CMS has duplicate identity or slug.")
    included = [row for row in opportunity_rows if qualified_public_record(row[0])]
    qualified = []
    unresolved = 0
    for record, chunk_url, chunk_sha in included:
        try:
            qualified.append(parse_opportunity_record(
                record, subject_labels, degree_labels,
                chunk_url=chunk_url, chunk_sha256=chunk_sha,
                subject_label_evidence=subject_label_evidence,
                degree_label_evidence=degree_label_evidence))
        except UnresolvedHandshakeFacet:
            unresolved += 1
    return qualified, len(opportunity_records), len(visible)-len(structurally_valid)+unresolved


def ensure_trailing_slash(url):
    return url if url.endswith("/") else f"{url}/"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _validate_asset_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port:
        raise ValueError("Handshake asset destination is outside scope.")
    if parsed.netloc == "joinhandshake.com" and parsed.path == "/ai/opportunities/":
        return
    if parsed.netloc == "framerusercontent.com" and (
        re.fullmatch(r"/sites/[A-Za-z0-9_-]+/[A-Za-z0-9_.@-]+\.mjs", parsed.path)
        or re.fullmatch(r"/cms/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+-chunk-default-\d+\.framercms", parsed.path)
    ):
        return
    raise ValueError("Handshake asset destination is outside scope.")


def _open_asset(url):
    _validate_asset_url(url)
    request = Request(url, headers=REQUEST_HEADERS)
    if current_source() is None:
        return build_opener(_NoRedirect()).open(request, timeout=60)
    return open_catalog(request, timeout=60)


def fetch_text(url):
    with _open_asset(url) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError("Handshake asset response left exact scope.")
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_bytes(url):
    with _open_asset(url) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError("Handshake asset response left exact scope.")
        return response.read()


def discover_cms_urls(html_text):
    urls = extract_framer_module_urls(html_text)
    if not urls or len(urls) > MAX_MODULES:
        raise ValueError("Handshake linked module count is absent or exceeds the bound.")
    cms_urls = {}
    names = (OPPORTUNITIES_COLLECTION, SUBJECT_FILTERS_COLLECTION,
             DEGREE_FILTERS_COLLECTION)
    for url in urls:
        module_text = fetch_text(url)
        declared = [name for name in names if f"displayName:`{name}`" in module_text]
        if len(declared) > 1:
            raise ValueError("Handshake module declares ambiguous CMS collections.")
        if not declared:
            continue
        name = declared[0]
        if name in cms_urls:
            raise ValueError("Handshake CMS collection declared twice.")
        cms_urls[name] = extract_collection_chunk_urls(
            module_text, linked_module_url=url, collection_name=name)
    if OPPORTUNITIES_COLLECTION not in cms_urls:
        raise ValueError("Could not find Handshake Opportunities Framer CMS chunks.")
    return cms_urls


def extract_framer_module_urls(html_text):
    urls = []
    for match in re.finditer(r'(?:href|src)="(https://framerusercontent\.com/sites/[^"]+\.mjs)"', html_text):
        url = unescape(match.group(1))
        parsed = urlsplit(url)
        if parsed.query or parsed.fragment or not re.fullmatch(r"/sites/[A-Za-z0-9_-]+/[A-Za-z0-9_.@-]+\.mjs", parsed.path):
            raise ValueError("Handshake module destination outside linked asset scope.")
        if url not in urls:
            urls.append(url)
    return urls


def extract_collection_chunk_urls(module_text, *, linked_module_url=None, collection_name=None):
    if collection_name is not None:
        marker = f'displayName:`{collection_name}`'
        if module_text.count(marker) != 1:
            raise ValueError('Handshake CMS collection declaration is ambiguous.')
        end = module_text.index(marker)
        start = module_text.rfind('collectionByLocaleId:{default:', 0, end)
        if start < 0 or 'displayName:`' in module_text[start:end]:
            raise ValueError('Handshake CMS collection has no bounded declaration.')
        # A linked module can contain other collections. Only the chunk list
        # attached to this exact displayName supplies its records.
        module_text = module_text[start:end]
    declarations = re.findall(
        r"new URL\(`\./([^`]+-chunk-default-(\d+)\.framercms)`,`([^`]+)`\)"
        r"\.href\.replace\(`/modules/`,`/cms/`\)",
        module_text,
    )
    if not declarations:
        raise ValueError("Handshake module has no declared CMS chunks.")
    prefixes = {name.rsplit("-chunk-default-", 1)[0]
                for name, _, _ in declarations}
    if len(prefixes) > 1:
        if collection_name is not None:
            raise ValueError('Handshake CMS collection has mixed chunk identities.')
        if linked_module_url is None:
            raise ValueError("Handshake module has ambiguous CMS collections.")
        filename = urlsplit(linked_module_url).path.rsplit('/', 1)[-1]
        linked_prefix = filename.split('.', 1)[0]
        if linked_prefix not in prefixes:
            raise ValueError("Handshake linked CMS collection identity is ambiguous.")
        declarations = [row for row in declarations
                        if row[0].rsplit("-chunk-default-", 1)[0] == linked_prefix]
    if len(declarations) > MAX_CHUNKS_PER_COLLECTION:
        raise ValueError("Handshake CMS chunk count exceeds bound.")
    chunks = {}
    collection_prefix = None
    collection_module = None
    for chunk_name, number, module_url in declarations:
        if not re.fullmatch(r"[A-Za-z0-9_-]+-chunk-default-\d+\.framercms", chunk_name):
            raise ValueError("Handshake CMS chunk identity invalid.")
        prefix = chunk_name.rsplit("-chunk-default-", 1)[0]
        if collection_prefix is None:
            collection_prefix, collection_module = prefix, module_url
        elif (prefix, module_url) != (collection_prefix, collection_module):
            raise ValueError("Handshake CMS chunks do not share collection identity.")
        origin = urlsplit(module_url)
        if linked_module_url is not None:
            linked = urlsplit(linked_module_url)
            linked_identity = re.fullmatch(
                r"/sites/[A-Za-z0-9_-]+/[A-Za-z0-9_.@-]+\.mjs",
                linked.path,
            )
            if (linked.scheme != "https" or linked.netloc != "framerusercontent.com"
                    or linked_identity is None
                    or not re.fullmatch(
                        r"/modules/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/"
                        + re.escape(prefix) + r"\.js", origin.path)):
                raise ValueError("Handshake CMS chunk is not bound to its linked module.")
        if origin.scheme != "https" or origin.netloc != "framerusercontent.com" or not origin.path.startswith("/modules/"):
            raise ValueError("Handshake CMS module origin invalid.")
        destination = urljoin(module_url, chunk_name).replace("/modules/", "/cms/")
        parsed = urlsplit(destination)
        if parsed.scheme != "https" or parsed.netloc != "framerusercontent.com" or not re.fullmatch(
            r"/cms/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+-chunk-default-\d+\.framercms", parsed.path
        ) or parsed.query or parsed.fragment:
            raise ValueError("Handshake CMS destination outside collection scope.")
        index = int(number)
        if index in chunks:
            raise ValueError("Handshake CMS chunk index is repeated.")
        chunks[index] = destination
    if set(chunks) != set(range(len(chunks))):
        raise ValueError("Handshake CMS chunk coverage has a gap.")
    return [chunks[index] for index in range(len(chunks))]


def extract_collection_chunk_url(module_text):
    return extract_collection_chunk_urls(module_text)[0]


def read_label_map(cms_url, title_field):
    if not cms_url:
        return {}, {}
    labels = {}
    evidence = {}
    for record, chunk_url, chunk_sha256 in read_framercms_rows(cms_url):
        record_id = clean_value(record.get(FIELD_ID))
        title = clean_value(record.get(title_field))
        if not record_id or not title or record_id in labels:
            raise ValueError('Handshake CMS filter label is missing or duplicated.')
        labels[record_id] = title
        evidence[record_id] = dict(cms_record=record, cms_chunk_url=chunk_url,
                                   cms_chunk_sha256=chunk_sha256)
    return labels, evidence


def read_framercms_records(cms_urls):
    return [record for record, _, _ in read_framercms_rows(cms_urls)]


def read_framercms_rows(cms_urls):
    if not isinstance(cms_urls, list) or not cms_urls or len(cms_urls) > MAX_CHUNKS_PER_COLLECTION:
        raise ValueError("Handshake CMS requires a bounded declared chunk set.")
    records = []
    for cms_url in cms_urls:
        parsed = urlsplit(cms_url)
        if parsed.scheme != "https" or parsed.netloc != "framerusercontent.com" or not re.fullmatch(
            r"/cms/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+-chunk-default-\d+\.framercms", parsed.path
        ) or parsed.query or parsed.fragment:
            raise ValueError("Handshake CMS destination outside collection scope.")
        raw = fetch_bytes(cms_url)
        decoder = FramerCmsDecoder(raw)
        chunk_records = decoder.read_records()
        if decoder.offset != len(decoder.data):
            raise ValueError("Handshake CMS chunk has trailing unsupported data.")
        digest = hashlib.sha256(raw).hexdigest()
        records.extend((record, cms_url, digest) for record in chunk_records)
    return records


def should_include_record(record):
    return (
        record.get(FIELD_SHOW_JOB) is True
        and bool(clean_value(record.get(FIELD_TITLE)))
        and bool(clean_value(record.get(FIELD_SLUG)))
        and bool(re.fullmatch(r"[A-Za-z0-9-]+", clean_value(record.get(FIELD_SLUG)) or ""))
        and bool(clean_value(record.get(FIELD_ID)))
    )


def qualified_public_record(record):
    description = clean_value(record.get(FIELD_DESCRIPTION)) or ''
    # The public Remote field alone does not encode residency, citizenship or
    # country limits in the prose. Hold such rows until a source-specific
    # geographic contract can preserve the actual condition in matching.
    if (countries_in_location(description) or regions_in_location(description)
            or re.search(r'\b(?:resid(?:e|ing|ency)|citizen(?:ship)?|work authorization|'
                         r'based in|located in|living in|north america|europe|asia|'
                         r'eligible countr(?:y|ies))\b', description, re.I)):
        return False
    if (not should_include_record(record) or record.get(FIELD_WORK_LOCATION) != 'Remote'
            or not re.search(r'\b(?:AI|LLM|large language models?|machine learning)\b',
                             description, re.I)):
        return False
    application = record.get(FIELD_APPLICATION)
    if type(application) is not str:
        return False
    parsed = urlsplit(application)
    try:
        query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
    except ValueError:
        return False
    return (parsed.scheme == 'https' and parsed.netloc == 'app.joinhandshake.com'
            and parsed.path == '/signup' and not parsed.fragment
            and set(query) == {'destination_hai_path', 'hai_job_id'}
            and query['destination_hai_path'] == ['/auth']
            and len(query['hai_job_id']) == 1
            and query['hai_job_id'][0].isdecimal())


def parse_opportunity_record(record, subject_labels, degree_labels, *,
                             chunk_url=None, chunk_sha256=None,
                             subject_label_evidence=None, degree_label_evidence=None):
    record_id = clean_value(record.get(FIELD_ID))
    title = clean_value(record.get(FIELD_TITLE))
    slug = clean_value(record.get(FIELD_SLUG))
    subjects = resolve_labels(record.get(FIELD_SUBJECT_FILTERS), subject_labels)
    degrees = resolve_labels(record.get(FIELD_DEGREE_FILTERS), degree_labels)
    expertise = "; ".join(subjects) if subjects else "Unknown"

    application = record.get(FIELD_APPLICATION)
    metadata = {
        "salary": record.get(FIELD_SALARY),
        "subjects": subjects,
        "degrees": degrees,
        "application_url": application,
        "subject_label_evidence": select_label_evidence(
            record.get(FIELD_SUBJECT_FILTERS), subject_label_evidence),
        "degree_label_evidence": select_label_evidence(
            record.get(FIELD_DEGREE_FILTERS), degree_label_evidence),
    }
    attestation = None
    if chunk_url is not None:
        if (not qualified_public_record(record) or type(chunk_sha256) is not str
                or re.fullmatch(r'[a-f0-9]{64}', chunk_sha256) is None):
            raise ValueError("Handshake record has no qualified chunk evidence.")
        job_id = parse_qs(urlsplit(application).query)['hai_job_id'][0]
        metadata.update(cms_record=record, cms_chunk_url=chunk_url,
                        cms_chunk_sha256=chunk_sha256, application_job_id=job_id)
        attestation = RecordPromotionAttestation(
            contract_id='handshake_public_cms_record_v1',
            body_observation=BODY_OBSERVATION_PRESENT,
            authority_evidence=dict(cms_id=record_id, slug=slug, title=title,
                application_url=application, application_job_id=job_id,
                cms_chunk_url=chunk_url, cms_chunk_sha256=chunk_sha256))
    return JobCandidate(
        external_id=f"handshake::{record_id}",
        title=title,
        location=clean_value(record.get(FIELD_WORK_LOCATION)) or "Unknown",
        url=f"{DETAIL_URL_PREFIX}/{slug}",
        department=expertise,
        expertise=expertise,
        commitment=build_commitment(record.get(FIELD_SALARY), degrees),
        opportunity_kind=OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY,
        availability_basis=AVAILABILITY_BASIS_PUBLIC_CMS,
        include_in_live_market_estimate=False,
        source_body=clean_value(record.get(FIELD_DESCRIPTION)),
        source_body_format='text/plain' if clean_value(record.get(FIELD_DESCRIPTION)) else None,
        source_metadata=metadata,
        record_promotion_attestation=attestation,
    )


def resolve_labels(ids, labels):
    if ids is None or ids == []:
        return []
    if (type(ids) not in (list, tuple) or any(type(item) is not str or not item
            or item not in labels for item in ids)):
        raise UnresolvedHandshakeFacet('Handshake CMS record has an unresolved filter reference.')
    return [labels[item_id] for item_id in ids]


def select_label_evidence(ids, evidence):
    if ids is None or ids == []:
        return {}
    if type(ids) not in (list, tuple) or evidence is None or any(
            item not in evidence for item in ids):
        # Unattested parser calls may inspect a record, but an attested
        # publication cannot silently drop its referenced condition labels.
        raise UnresolvedHandshakeFacet('Handshake CMS label evidence is incomplete.')
    return {item: evidence[item] for item in ids}


def build_commitment(salary, degrees):
    parts = []
    if degrees:
        parts.append(f"Degree filters: {', '.join(degrees)}")
    return "; ".join(parts) or None


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

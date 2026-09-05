"""Explicit, identity-bound detail recovery; never called by a matching request.

Detail retrieval is content evidence, not a catalog refresh or application test.
Only Alignerr and micro1's existing official per-job pages are supported.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener

from wahojobs.crawler.source_content import first_text
from wahojobs.profiles.countries import normalize_country


DETAIL_KEY = "wahojobs_source_detail_v1"
MAX_DETAIL_BYTES = 2_000_000


@dataclass(frozen=True)
class DetailResponse:
    url: str
    body: bytes
    observed_at: str
    status: int = 200


def validate_detail_url(provider, external_id, url):
    parsed = urlparse(url)
    host, prefix = {
        "alignerr": ("www.alignerr.com", "/jobs/"),
        "micro1": ("jobs.micro1.ai", "/post/"),
    }[provider]
    if (parsed.scheme != "https" or parsed.hostname != host
            or parsed.port is not None or parsed.username or parsed.password
            or parsed.fragment or not re.fullmatch(r"[a-zA-Z0-9-]+", external_id)
            or parsed.path != prefix + external_id):
        raise ValueError("Detail URL does not match the official source record")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_detail(provider, candidate):
    """One explicit GET, no redirects/retries/assets; the caller preserves failure."""
    validate_detail_url(provider, candidate.external_id, candidate.url)
    request = Request(candidate.url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
        "Accept": "text/html",
    })
    with build_opener(_NoRedirect()).open(request, timeout=25) as response:
        body = response.read(MAX_DETAIL_BYTES + 1)
        if len(body) > MAX_DETAIL_BYTES:
            raise ValueError("Detail response exceeds size limit")
        return DetailResponse(candidate.url, body,
                              datetime.now(timezone.utc).isoformat(), response.status)


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.current = [dict(attrs), ""]
            self.scripts.append(self.current)

    def handle_endtag(self, tag):
        if tag == "script":
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data


def _scripts(response):
    observed = datetime.fromisoformat(response.observed_at.replace("Z", "+00:00"))
    if observed.tzinfo is None or response.status != 200:
        raise ValueError("Detail response is not a dated successful observation")
    if not response.body or len(response.body) > MAX_DETAIL_BYTES:
        raise ValueError("Detail response is empty or oversized")
    parser = _Scripts()
    parser.feed(response.body.decode("utf-8"))
    return parser.scripts


def _flight_values(scripts):
    """Read JSON/text records from Next's captured stream without executing JS.

Text frame lengths count UTF-8 bytes, not Python characters. Incomplete frames
are rejected rather than accepting a truncated description or screening list.
"""
    chunks = []
    for _attrs, text in scripts:
        match = re.fullmatch(r"self\.__next_f\.push\((.+)\);?", text.strip(), re.S)
        if match:
            value = json.loads(match[1])
            if len(value) == 2 and value[0] == 1 and isinstance(value[1], str):
                chunks.append(value[1])
    stream = "".join(chunks).encode("utf-8")
    records, offset = {}, 0
    while offset < len(stream):
        match = re.match(rb"([a-f0-9]+):", stream[offset:])
        if not match:
            raise ValueError("Malformed or incomplete detail stream")
        key = match[1].decode()
        offset += match.end()
        length = re.match(rb"T([a-f0-9]+),", stream[offset:])
        if length:
            offset += length.end()
            end = offset + int(length[1], 16)
            if end > len(stream):
                raise ValueError("Truncated detail text frame")
            value = stream[offset:end].decode("utf-8")
            offset = end
        else:
            end = stream.find(b"\n", offset)
            if end == -1:
                raise ValueError("Truncated detail JSON frame")
            raw = stream[offset:end].decode("utf-8")
            value = json.loads(raw) if raw.startswith(("[", "{")) else None
            offset = end + 1
        if key in records:
            raise ValueError("Duplicate detail stream identity")
        records[key] = value
    return records


def _objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from _objects(item)


def _resolve_text(value, frames):
    if isinstance(value, str) and re.fullmatch(r"\$[a-f0-9]+", value):
        value = frames.get(value[1:])
    if not isinstance(value, str) or not value.strip() or value.startswith("$"):
        raise ValueError("Missing detail description")
    return value


def _alignerr_applicant_location(body, body_format, original_location):
    """Forward a whole applicant-location bullet to the existing location gate.

The detail record's unqualified city is not an applicant restriction. Only a
complete country-alternatives clause in Who You Are / Requirements is used.
Unknown tokens, conditional/negative wording and multiple clauses stay raw.
"""
    if body_format != "text/markdown":
        return original_location, None
    section, clauses = "", []
    for number, line in enumerate(body.splitlines(), 1):
        if line.startswith("#"):
            section = line.strip("# :").casefold()
        elif section in {"who you are", "requirements"}:
            match = re.fullmatch(r"\s*[-*] (?:Based in|Must be based in) (.+?)\s*", line)
            if match:
                clauses.append((number, line, match[1]))
    if len(clauses) != 1:
        return original_location, None
    number, quote, raw = clauses[0]
    tokens = re.split(r",\s*(?:or\s+)?|\s+or\s+", raw.rstrip("."))
    countries = []
    try:
        for token in tokens:
            token = re.sub(r"^the\s+", "", token.strip(), flags=re.I)
            token = {"U.S.": "United States", "U.S": "United States",
                     "U.K.": "United Kingdom", "U.K": "United Kingdom"}.get(token, token)
            countries.append(normalize_country(token))
    except ValueError:
        return original_location, None
    prefix = "Remote - " if str(original_location).casefold() == "remote" else ""
    return prefix + ", ".join(countries), {
        "source_field": f"props.pageProps.job.longDescription:line {number}",
        "source_quote": quote, "countries": countries,
    }


def recover_detail(provider, candidate, response):
    """Return a new candidate only on complete, exact-record detail evidence.

Raw provider objects live in metadata alongside the original listing metadata.
Dates from different source representations are not silently substituted.
"""
    validate_detail_url(provider, candidate.external_id, response.url)
    if response.url != candidate.url:
        raise ValueError("Detail response belongs to another URL")
    scripts = _scripts(response)
    if provider == "alignerr":
        packets = [json.loads(text) for attrs, text in scripts
                   if attrs.get("id") == "__NEXT_DATA__"]
        if len(packets) != 1:
            raise ValueError("Missing or duplicate Alignerr detail packet")
        record = packets[0].get("props", {}).get("pageProps", {}).get("job")
        if not isinstance(record, dict) or record.get("id") != candidate.external_id:
            raise ValueError("Alignerr detail identity mismatch")
        if record.get("name") != candidate.title or record.get("isActive") is not True:
            raise ValueError("Alignerr detail is inactive or has a conflicting title")
        body = first_text(record, ("longDescription", "htmlLongDescription"))
        field = "longDescription" if first_text(record, ("longDescription",)) else "htmlLongDescription"
        if not body or body.rstrip().endswith(("...", "…")):
            raise ValueError("Alignerr detail missing or visibly truncated")
        body_format = "text/markdown" if field == "longDescription" else "text/html"
        location, location_evidence = _alignerr_applicant_location(
            body, body_format, candidate.location)
        evidence = {"field": "props.pageProps.job." + field, "record": record,
                    "applicant_location_projection": location_evidence}
    elif provider == "micro1":
        frames = _flight_values(scripts)
        records = [obj for value in frames.values() for obj in _objects(value)
                   if obj.get("id") == candidate.external_id
                   and isinstance(obj.get("data"), dict)
                   and obj["data"].get("client_job_id") == candidate.external_id]
        if len(records) != 1:
            raise ValueError("Missing or duplicate micro1 detail record")
        props = records[0]
        record = props["data"]
        if record.get("job_role_name") != candidate.title or record.get("job_status") != "open":
            raise ValueError("micro1 detail is closed or has a conflicting title")
        body = _resolve_text(record.get("job_description"), frames)
        if body.rstrip().endswith(("...", "…")):
            raise ValueError("micro1 description visibly truncated")
        questions = props.get("job_qualifying_question_list")
        if not isinstance(questions, list) or any(
            not isinstance(q, dict) or not isinstance(q.get("question_text"), str)
            or not q["question_text"].strip() for q in questions
        ):
            raise ValueError("Missing or malformed micro1 screening questions")
        if questions:
            body += "<h2>Application screening questions</h2><ol>" + "".join(
                "<li>" + escape(q["question_text"]) + "</li>" for q in questions
            ) + "</ol>"
        # Keep source-generated JSON-LD separate from the actual form record.
        jsonld = []
        for value in frames.values():
            if isinstance(value, str) and value.startswith("{"):
                parsed = json.loads(value)
                if parsed.get("@type") == "JobPosting":
                    jsonld.append(parsed)
        public_record = {k: v for k, v in record.items() if k != "client_details"}
        public_record["job_description"] = _resolve_text(record.get("job_description"), frames)
        evidence = {"field": "data.job_description", "record": public_record,
                    "screening_questions": questions, "source_jsonld": jsonld}
        body_format = "text/html"
        # No default: an explicit description header may supply a missing field.
        # Its quote/provenance stays separate from the unchanged null field.
        location = first_text(record, ("location_type",))
    else:
        raise ValueError("Unsupported detail provider")
    from wahojobs.opportunity_enrichment import source_body_paragraphs
    display_text = (body if body_format != "text/html" else
                    "\n\n".join(source_body_paragraphs(body, body_format)))
    if provider == "micro1" and location is None:
        headers = [p for p in source_body_paragraphs(evidence["record"]["job_description"], "text/html")[:8]
                   if p.startswith("Location:")]
        if headers == ["Location: Remote"]:
            location = "Remote"
            evidence["location_projection"] = {"source_field": "data.job_description",
                                                "source_quote": headers[0], "value": location}
    if provider == "alignerr":
        display_text += "\n\nOther published fields (not additional applicant requirements):\n" + "\n".join(
            f"{label}: {record[key]}" for key, label in (
                ("location", "Listing location"), ("jobType", "Engagement type"),
                ("lowerBoundHourlyRate", "Hourly range minimum"),
                ("upperBoundHourlyRate", "Hourly range maximum")) if record.get(key) is not None)
    else:
        display_text += "\n\nOther published fields (read alongside the description):\n" + "\n".join(
            f"{label}: {record[key]}" for key, label in (
                ("ideal_hourly_rate", "Published hourly-rate fields"),
                ("location_type", "Listing location type"),
                ("required_availability", "Required availability")) if record.get(key) is not None)
        for document in evidence["source_jsonld"]:
            countries = document.get("applicantLocationRequirements")
            if isinstance(countries, list):
                names = [c.get("name") for c in countries if isinstance(c, dict) and isinstance(c.get("name"), str)]
                display_text += "\nSource page's structured applicant-location list: " + ", ".join(names)
    metadata = dict(candidate.source_metadata or {})
    metadata[DETAIL_KEY] = {
        "version": 1, "url": response.url, "observed_at": response.observed_at,
        "http_status": response.status, "response_sha256": sha256(response.body).hexdigest(),
        "provider": provider, "external_id": candidate.external_id,
        "application_acceptance_verified": False, "display_text": display_text, **evidence,
    }
    return replace(candidate, source_body=body, source_body_format=body_format,
                   source_metadata=metadata, location=location)


def reprocess_saved_detail(connection, job_id, response):
    """Reprocess one accepted catalog record using a separately dated detail.

    Retain the historical catalog capture as provenance, but use a separately
    dated, content-only attestation: no crawl run, lifecycle observation, rollup
    or removal is written. Invalid/empty detail raises before any write. Existing
    material degradation and timestamp conflict checks remain in force.
"""
    from wahojobs.crawler.types import (JobCandidate, RecordPromotionAttestation,
                                       PROVIDER_DETAIL_RECORD_CONTRACT_ID)
    from wahojobs.db.repository import get_job_source_capture_evidence, upsert_job_source_content
    from wahojobs.source_capture import SourceCaptureContext
    job = connection.execute(
        "SELECT j.*, c.slug AS provider FROM jobs j JOIN companies c ON c.id=j.company_id WHERE j.id=?",
        (job_id,),
    ).fetchone()
    if job is None or job["provider"] not in {"alignerr", "micro1"}:
        raise ValueError("Unsupported detail record")
    # Verify the accepted chain before using its catalog authority context.
    get_job_source_capture_evidence(connection, job_id)
    capture = connection.execute(
        "SELECT sc.* FROM job_source_content_acceptances a JOIN job_source_content_captures sc "
        "ON sc.id=a.accepted_capture_id WHERE a.job_id=?", (job_id,),
    ).fetchone()
    if capture is None:
        raise ValueError("No accepted catalog context for detail reprocessing")
    fields = {name: job[name] for name in (
        "title", "location", "url", "external_id", "department", "expertise", "commitment",
        "opportunity_kind", "availability_basis", "include_in_live_market_estimate", "source_hash")}
    candidate = JobCandidate(**fields, source_body=capture["body"],
                             source_body_format=capture["body_format"],
                             source_metadata=json.loads(capture["metadata_json"]),
                             source_updated_at=capture["source_updated_at"])
    recovered = recover_detail(job["provider"], candidate, response)
    # Keep the first listing capture as the catalog baseline on repeated recovery.
    previous = (candidate.source_metadata or {}).get(DETAIL_KEY, {})
    recovered.source_metadata[DETAIL_KEY]["catalog_baseline_capture_id"] = previous.get(
        "catalog_baseline_capture_id", capture["id"])
    recovered.source_metadata[DETAIL_KEY]["catalog_observed_at"] = previous.get(
        "catalog_observed_at", capture["observed_at"])
    detail = recovered.source_metadata[DETAIL_KEY]
    recovered = replace(recovered, record_promotion_attestation=RecordPromotionAttestation(
        PROVIDER_DETAIL_RECORD_CONTRACT_ID, "present", {
            "url": response.url, "external_id": recovered.external_id,
            "response_sha256": detail["response_sha256"],
            "observed_at": response.observed_at, "content_only": True,
        }))
    context = SourceCaptureContext(None, "partial", False, False, False, False,
                                   1, 1, 1, 0, PROVIDER_DETAIL_RECORD_CONTRACT_ID,
                                   PROVIDER_DETAIL_RECORD_CONTRACT_ID)
    return upsert_job_source_content(connection, job_id, job["provider"],
                                     capture["source_type"], recovered, response.observed_at,
                                     capture_context=context)

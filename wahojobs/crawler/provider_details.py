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
    from wahojobs.crawler.local_inventory import reserve_http_request, audit_http_response, audit_http_error
    entry = reserve_http_request(request, detail=True)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=25) as response:
            if entry is not None: entry['status'] = response.status
            body = response.read(MAX_DETAIL_BYTES + 1)
            audit_http_response(entry, body=body, capture_complete=len(body) <= MAX_DETAIL_BYTES)
            if len(body) > MAX_DETAIL_BYTES:
                raise ValueError("Detail response exceeds size limit")
            return DetailResponse(candidate.url, body,
                                  datetime.now(timezone.utc).isoformat(), response.status)
    except Exception as exc:
        if entry is not None:
            entry.update(status=getattr(exc, 'code', entry.get('status')), error=type(exc).__name__)
        audit_http_error(entry, exc)
        raise


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
    return _prepare_detail_geography(replace(candidate, source_body=body, source_body_format=body_format,
                   source_metadata=metadata, location=location))


def _prepare_detail_geography(candidate):
    from copy import deepcopy
    from wahojobs.matching.source_geography import prepare_applicant_location_support
    metadata = deepcopy(candidate.source_metadata)
    detail = metadata[DETAIL_KEY]
    if candidate.source_body_format == 'text/markdown':
        support = prepare_applicant_location_support(candidate.source_body, detail['field'])
        if support:
            detail['applicant_location_support'] = support
        else:
            detail.pop('applicant_location_support', None)
    return replace(candidate, source_metadata=metadata)


def reprocess_saved_detail(connection, job_id, response=None, *, catalog_capture_id=None):
    """Reprocess one accepted catalog record using a separately dated detail.

    Retain the historical catalog capture as provenance, but use a separately
    dated, content-only attestation: no crawl run, lifecycle observation, rollup
    or removal is written. Invalid/empty detail raises before any write. Existing
    material degradation and timestamp conflict checks remain in force. With
    no response, reprepare an already accepted, identity-verified Markdown
    detail using its original capture and attestation, without fabricating a
    new HTTP observation. Existing source-promotion checks still apply.
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
    previous = (candidate.source_metadata or {}).get(DETAIL_KEY, {})
    if catalog_capture_id is not None:
        # The ordinary refresh may have held a new catalog teaser to protect a
        # fuller accepted detail. Use its verified record fields for the new
        # detail, retaining the original catalog provenance below.
        from wahojobs.db.repository import (
            _verify_stored_source_material, _captured_semantic_material,
            _capture_context_from_row, _verify_capture_crawl_provenance,
        )
        current = connection.execute("SELECT * FROM job_source_content_captures WHERE id=? AND job_id=? AND crawl_run_id IS NOT NULL", (catalog_capture_id, job_id)).fetchone()
        if current is None:
            raise ValueError("Missing matching catalog capture")
        _, metadata = _verify_stored_source_material(current, "catalog")
        catalog_job = dict(job, company_slug=job['provider'])
        _captured_semantic_material(catalog_job, current, metadata)
        _verify_capture_crawl_provenance(connection, catalog_job, current, _capture_context_from_row(current))
        current_fields = json.loads(current['semantic_job_fields_json'])
        candidate = JobCandidate(**current_fields, source_body=current['body'], source_body_format=current['body_format'], source_metadata=metadata, source_updated_at=current['source_updated_at'])
    if response is None:
        if catalog_capture_id is not None:
            raise ValueError('Accepted-detail preparation cannot substitute a catalog capture')
        validate_detail_url(job['provider'], candidate.external_id, candidate.url)
        record = previous.get('record') or {}
        field = previous.get('field', '')
        if (previous.get('version') != 1 or previous.get('provider') != job['provider']
                or previous.get('external_id') != candidate.external_id or previous.get('url') != candidate.url
                or candidate.source_body_format != 'text/markdown'
                or field != 'props.pageProps.job.longDescription'
                or record.get('id') != candidate.external_id or record.get('name') != candidate.title
                or first_text(record, ('longDescription',)) != candidate.source_body
                or capture['record_promotion_contract_id'] != PROVIDER_DETAIL_RECORD_CONTRACT_ID):
            raise ValueError('No identity-verified accepted Markdown detail to prepare')
        recovered = _prepare_detail_geography(candidate)
    else:
        recovered = recover_detail(job["provider"], candidate, response)
    # Keep the first listing capture as the catalog baseline on repeated recovery.
    recovered.source_metadata[DETAIL_KEY]["catalog_baseline_capture_id"] = previous.get(
        "catalog_baseline_capture_id", capture["id"])
    recovered.source_metadata[DETAIL_KEY]["catalog_observed_at"] = previous.get(
        "catalog_observed_at", capture["observed_at"])
    detail = recovered.source_metadata[DETAIL_KEY]
    recovered = replace(recovered, record_promotion_attestation=RecordPromotionAttestation(
        PROVIDER_DETAIL_RECORD_CONTRACT_ID, "present", {
            "url": detail['url'], "external_id": recovered.external_id,
            "response_sha256": detail["response_sha256"],
            "observed_at": detail['observed_at'], "content_only": True,
        }))
    context = SourceCaptureContext(None, "partial", False, False, False, False,
                                   1, 1, 1, 0, PROVIDER_DETAIL_RECORD_CONTRACT_ID,
                                   PROVIDER_DETAIL_RECORD_CONTRACT_ID)
    return upsert_job_source_content(connection, job_id, job["provider"],
                                     capture["source_type"], recovered, detail['observed_at'],
                                     capture_context=context)


def update_returned_details(connection, provider, company_id, crawl_run_id, candidates, *, mode="needed"):
    """Ordinary post-catalog content update; never fetch for absent records.

    Reuse means retaining dated accepted evidence, not declaring that a public
    page is unchanged now. `all` explicitly checks detail-only changes.
    """
    from wahojobs.db.repository import get_job_source_capture_evidence
    from wahojobs.canonical.service import sync_alignerr_canonical_opportunities, sync_micro1_canonical_opportunities, sync_fallback_canonical_opportunities
    from wahojobs.opportunity_enrichment import enrich_selected_opportunities
    from wahojobs.crawler.local_inventory import RequestBudgetExceeded, record_detail_recovery
    counts = dict(reused=0, accepted=0, held=0, failed=0, pending=0)
    seen = set()
    pending = []
    for candidate in candidates:
        if candidate.external_id in seen:
            continue
        seen.add(candidate.external_id)
        row = connection.execute("SELECT j.id, a.accepted_capture_id, s.metadata_json FROM jobs j LEFT JOIN job_source_contents s ON s.job_id=j.id LEFT JOIN job_source_content_acceptances a ON a.job_id=j.id WHERE j.company_id=? AND j.external_id=? AND j.is_active=1", (company_id, candidate.external_id)).fetchone()
        if row is None:
            continue
        current = connection.execute("SELECT * FROM job_source_content_captures WHERE job_id=? AND crawl_run_id=? ORDER BY id DESC LIMIT 1", (row['id'], crawl_run_id)).fetchone()
        if current is None:
            raise ValueError("Returned detail has no catalog observation")
        get_job_source_capture_evidence(connection, row['id'])
        detail = json.loads(row['metadata_json'] or '{}').get(DETAIL_KEY, {})
        baseline = connection.execute("SELECT semantic_material_sha256,source_updated_at FROM job_source_content_captures WHERE job_id=? AND crawl_run_id IS NOT NULL AND id<? ORDER BY id DESC LIMIT 1", (row['id'], row['accepted_capture_id'] or 0)).fetchone()
        same_detail = (detail.get('url') == candidate.url and detail.get('external_id') == candidate.external_id
                       and detail.get('provider') == provider and detail.get('version') == 1)
        if (mode == "needed" and same_detail
                and baseline is not None and baseline['semantic_material_sha256'] == current['semantic_material_sha256']
                and baseline['source_updated_at'] == current['source_updated_at']):
            counts['reused'] += 1
            continue
        # Retain exact-variant compatibility checks. Missing usable detail takes
        # precedence over a recheck of accepted detail; catalog order breaks ties.
        pending.append((bool(same_detail and detail.get('display_text')), candidate, row['id'], current['id']))
    for _, candidate, job_id, capture_id in sorted(pending, key=lambda item: item[0]):
        try:
            # Short reads have finished. No write transaction across HTTP.
            if connection.in_transaction:
                raise RuntimeError("Detail retrieval cannot hold a write transaction")
            response = fetch_detail(provider, candidate)
            with connection:
                outcome = reprocess_saved_detail(connection, job_id, response, catalog_capture_id=capture_id)
                if outcome.accepted:
                    sync = sync_alignerr_canonical_opportunities if provider == 'alignerr' else sync_micro1_canonical_opportunities
                    sync(connection, company_id)
                    sync_fallback_canonical_opportunities(connection, company_id)
                    canonical = connection.execute("SELECT canonical_opportunity_id FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
                    if canonical is not None:
                        enrich_selected_opportunities(connection, {canonical}, llm_client=None)
            counts['accepted' if outcome.accepted else 'held'] += 1
        except RequestBudgetExceeded:
            counts['pending'] += 1
        except (OSError, ValueError):
            # The original accepted content and source clocks remain intact.
            # Corrupt accepted provenance / database failures are not swallowed.
            counts['failed'] += 1
    record_detail_recovery(provider, counts)
    return "Detail recovery (content only): " + ", ".join(f"{key}={value}" for key, value in counts.items())

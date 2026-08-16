import hashlib
import json
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from wahojobs.crawler.types import (
    CompanyCrawlResult,
    JobCandidate,
    ProviderOutcome,
)
from wahojobs.crawler.source_content import nonempty_metadata, selected_metadata


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json",
    "Referer": "https://www.alignerr.com/jobs",
}
REQUEST_TIMEOUT_SECONDS = 90
MAX_PAGE_SIZE = 120
MAX_PAGES = 100
MAX_RECORDS = 20_000
ALIGNERR_ORIGIN = "https://www.alignerr.com"

V1_PAYLOAD_SHAPE = "alignerr:v1:legacy-array"
V2_PAYLOAD_SHAPE = "alignerr:v2:paginated-object"
V2_TOP_LEVEL_FIELDS = {
    "jobs": "list",
    "limit": "integer",
    "offset": "integer",
    "total": "integer",
}
V1_REQUIRED_RECORD_FIELDS = {
    "id": "string",
    "name": "string",
    "isActive": "boolean",
}
V1_OPTIONAL_RECORD_FIELDS = {
    "absolute_url": "string|null",
    "category": "string|null",
    "jobType": "string|null",
    "location": "string|null",
}
V2_REQUIRED_RECORD_FIELDS = {
    "applyUrl": "string",
    "category": "string",
    "id": "string",
    "location": "string",
    "title": "string",
}
V2_OPTIONAL_RECORD_FIELDS = {
    "description": "string|null",
    "originalCategory": "string|null",
    "pay": "string|null",
}
SENSITIVE_ADDITIVE_ENVELOPE_FIELDS = {"error", "errors", "status", "success"}
SENSITIVE_ADDITIVE_RECORD_FIELDS = {
    "deletedAt",
    "isActive",
    "isPrivate",
    "isPublic",
    "public",
    "status",
}


def fetch_alignerr_snapshot(api_url):
    first_payload = request_json(add_pagination(api_url, MAX_PAGE_SIZE, 0))
    if isinstance(first_payload, list):
        return parse_legacy_snapshot(first_payload, api_url)
    if not isinstance(first_payload, dict):
        return contract_drift_result(
            api_url,
            f"Unknown Alignerr root type: {type_name(first_payload)}.",
            payload_shape=f"root:{type_name(first_payload)}",
        )
    return fetch_paginated_v2_snapshot(api_url, first_payload)


def request_json(url):
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")
    return json.loads(payload)


def fetch_paginated_v2_snapshot(api_url, first_payload):
    # The public v2 listing has no activity flag. Presence is active evidence only
    # after every declared page has been fetched and validated.
    contract_error = validate_v2_envelope(first_payload)
    if contract_error:
        return contract_drift_result(
            api_url,
            contract_error,
            payload_shape=describe_root_shape(first_payload),
        )

    expected_total = first_payload["total"]
    page_size = first_payload["limit"]
    if first_payload["offset"] != 0:
        return partial_v2_result(
            api_url,
            [],
            0,
            0,
            [f"First page returned offset {first_payload['offset']} instead of 0."],
        )
    if expected_total > MAX_RECORDS:
        return partial_v2_result(
            api_url,
            [],
            0,
            0,
            [
                f"Declared total {expected_total} exceeds the safety bound "
                f"of {MAX_RECORDS} records."
            ],
        )

    jobs_by_id = {}
    raw_record_count = 0
    rejected_record_count = 0
    warnings = []
    seen_page_signatures = set()
    requested_offset = 0
    page_number = 0
    payload = first_payload

    while True:
        page_number += 1
        if page_number > MAX_PAGES:
            warnings.append(f"Pagination exceeded the safety bound of {MAX_PAGES} pages.")
            return partial_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                rejected_record_count,
                warnings,
            )

        contract_error = validate_v2_envelope(payload)
        if contract_error:
            return contract_drift_result(
                api_url,
                f"Page at offset {requested_offset}: {contract_error}",
                payload_shape=describe_root_shape(payload),
            )
        if payload["total"] != expected_total:
            warnings.append(
                f"Declared total changed from {expected_total} to {payload['total']} "
                f"at offset {requested_offset}."
            )
            return partial_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                rejected_record_count,
                warnings,
            )
        if payload["limit"] != page_size:
            warnings.append(
                f"Page limit changed from {page_size} to {payload['limit']} "
                f"at offset {requested_offset}."
            )
            return partial_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                rejected_record_count,
                warnings,
            )
        if payload["offset"] != requested_offset:
            warnings.append(
                f"Requested offset {requested_offset}, but response reported "
                f"{payload['offset']}."
            )
            return partial_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                rejected_record_count,
                warnings,
            )

        page_records = payload["jobs"]
        for warning in additive_field_warnings(payload):
            if warning not in warnings:
                warnings.append(warning)
        raw_record_count += len(page_records)
        if raw_record_count > MAX_RECORDS:
            warnings.append(f"Fetched records exceeded the safety bound of {MAX_RECORDS}.")
            return partial_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                rejected_record_count,
                warnings,
            )
        if len(page_records) > page_size:
            warnings.append(
                f"Page at offset {requested_offset} returned {len(page_records)} "
                f"records for declared limit {page_size}."
            )
            return partial_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                rejected_record_count,
                warnings,
            )

        signature = page_signature(page_records)
        if signature in seen_page_signatures and page_records:
            warnings.append(f"Duplicate page detected at offset {requested_offset}.")
            return partial_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                rejected_record_count,
                warnings,
            )
        seen_page_signatures.add(signature)

        unique_before = len(jobs_by_id)
        for index, record in enumerate(page_records):
            job, error = parse_v2_record(record)
            if error:
                rejected_record_count += 1
                warnings.append(
                    f"Rejected record at offset {requested_offset}, index {index}: {error}"
                )
                continue
            if job.external_id in jobs_by_id:
                rejected_record_count += 1
                warnings.append(
                    f"Duplicate job id {job.external_id!r} at offset {requested_offset}."
                )
                continue
            jobs_by_id[job.external_id] = job

        if len(jobs_by_id) == unique_before and page_records:
            warnings.append(f"Pagination made no unique-record progress at offset {requested_offset}.")
        if rejected_record_count:
            return partial_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                rejected_record_count,
                warnings,
            )
        if len(jobs_by_id) > expected_total:
            warnings.append(
                f"Collected {len(jobs_by_id)} unique jobs, exceeding declared "
                f"total {expected_total}."
            )
            return partial_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                rejected_record_count,
                warnings,
            )
        if len(jobs_by_id) == expected_total:
            return complete_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                warnings,
            )
        if not page_records:
            warnings.append(
                f"Unexpected empty page at offset {requested_offset} before "
                f"declared total {expected_total} was reached."
            )
            return partial_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                rejected_record_count,
                warnings,
            )
        if len(jobs_by_id) == unique_before:
            return partial_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                rejected_record_count,
                warnings,
            )
        if len(page_records) < page_size:
            warnings.append(
                f"Premature short page at offset {requested_offset}: returned "
                f"{len(page_records)} of {page_size} before total {expected_total}."
            )
            return partial_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                rejected_record_count,
                warnings,
            )

        next_offset = requested_offset + page_size
        if next_offset <= requested_offset:
            warnings.append(f"Pagination offset did not advance from {requested_offset}.")
            return partial_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                rejected_record_count,
                warnings,
            )
        if page_number >= MAX_PAGES:
            warnings.append(f"Pagination reached the safety bound of {MAX_PAGES} pages.")
            return partial_v2_result(
                api_url,
                list(jobs_by_id.values()),
                raw_record_count,
                rejected_record_count,
                warnings,
            )
        requested_offset = next_offset
        payload = request_json(add_pagination(api_url, page_size, requested_offset))


def parse_legacy_snapshot(records, api_url):
    jobs = []
    rejected = 0
    warnings = []
    seen_ids = set()
    for index, record in enumerate(records):
        job, active, error = parse_v1_record(record)
        if error:
            rejected += 1
            warnings.append(f"Rejected legacy record at index {index}: {error}")
            continue
        if not active:
            continue
        if job.external_id in seen_ids:
            rejected += 1
            warnings.append(f"Duplicate legacy job id {job.external_id!r} at index {index}.")
            continue
        seen_ids.add(job.external_id)
        jobs.append(job)

    if rejected:
        return CompanyCrawlResult(
            jobs=jobs,
            used_sample_data=False,
            source_message="Alignerr legacy v1 payload was recognized but incomplete.",
            source_type="alignerr-marketplace",
            outcome=ProviderOutcome.PARTIAL,
            snapshot_complete=False,
            pagination_complete=False,
            payload_shape=V1_PAYLOAD_SHAPE,
            raw_record_count=len(records),
            normalized_record_count=len(jobs),
            rejected_record_count=rejected,
            warnings=tuple(warnings),
            schema_fingerprint=schema_fingerprint("v1"),
        )

    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_message=f"Fetched complete Alignerr legacy v1 snapshot: {api_url}",
        source_type="alignerr-marketplace",
        outcome=ProviderOutcome.SUCCESS,
        snapshot_complete=True,
        pagination_complete=True,
        empty_snapshot_validated=False,
        payload_shape=V1_PAYLOAD_SHAPE,
        raw_record_count=len(records),
        normalized_record_count=len(jobs),
        rejected_record_count=0,
        warnings=(),
        schema_fingerprint=schema_fingerprint("v1"),
    )


def parse_v1_record(record):
    error = validate_record_fields(
        record,
        V1_REQUIRED_RECORD_FIELDS,
        V1_OPTIONAL_RECORD_FIELDS,
    )
    if error:
        return None, False, error

    job_id = clean_value(record["id"])
    name = clean_value(record["name"])
    if not job_id or not name:
        return None, False, "id and name must be non-empty strings"
    if not record["isActive"]:
        return None, False, None

    raw_url = clean_value(record.get("absolute_url"))
    if raw_url:
        url = normalize_application_url(raw_url, expected_job_id=job_id)
    else:
        url = normalize_application_url(
            f"/jobs/{job_id}",
            expected_job_id=job_id,
        )
    if not url:
        return None, False, "absolute_url was not a usable HTTP(S) job URL"

    category = clean_value(record.get("category")) or "Unknown"
    return (
        JobCandidate(
            external_id=job_id,
            title=name,
            location=clean_value(record.get("location")) or "Remote",
            url=url,
            department=category,
            expertise=category,
            commitment=clean_value(record.get("jobType")),
        ),
        True,
        None,
    )


def parse_v2_record(record):
    error = validate_record_fields(
        record,
        V2_REQUIRED_RECORD_FIELDS,
        V2_OPTIONAL_RECORD_FIELDS,
    )
    if error:
        return None, error

    values = {
        field: clean_value(record[field])
        for field in V2_REQUIRED_RECORD_FIELDS
    }
    if any(not value for value in values.values()):
        return None, "required string fields must be non-empty"
    url = normalize_application_url(
        values["applyUrl"],
        expected_job_id=values["id"],
    )
    if not url:
        return None, "applyUrl was not a usable HTTP(S) job URL"

    return (
        JobCandidate(
            external_id=values["id"],
            title=values["title"],
            location=values["location"],
            url=url,
            department=values["category"],
            expertise=values["category"],
            commitment=None,
            source_body=clean_value(record.get("description")),
            source_body_format=(
                "text/plain" if clean_value(record.get("description")) else None
            ),
            source_metadata=nonempty_metadata(
                selected_metadata(record, ("originalCategory", "pay"))
            ),
        ),
        None,
    )


def validate_v2_envelope(payload):
    missing = [field for field in V2_TOP_LEVEL_FIELDS if field not in payload]
    if missing:
        return f"Missing required v2 pagination field(s): {', '.join(sorted(missing))}."
    if not isinstance(payload["jobs"], list):
        return "v2 field 'jobs' must be a list."
    sensitive_additions = sorted(
        set(payload) & SENSITIVE_ADDITIVE_ENVELOPE_FIELDS
    )
    if sensitive_additions:
        return (
            "Unknown safety-sensitive v2 envelope field(s): "
            + ", ".join(sensitive_additions)
            + "."
        )
    for field in ("limit", "offset", "total"):
        value = payload[field]
        if not is_integer(value):
            return f"v2 field {field!r} must be an integer."
        if value < 0:
            return f"v2 field {field!r} cannot be negative."
    if payload["limit"] <= 0:
        return "v2 field 'limit' must be greater than zero."
    if payload["limit"] > MAX_PAGE_SIZE:
        return (
            f"v2 field 'limit' exceeds the supported maximum of {MAX_PAGE_SIZE}."
        )
    return None


def validate_record_fields(record, required_fields, optional_fields):
    if not isinstance(record, dict):
        return f"record must be an object, got {type_name(record)}"
    missing = [field for field in required_fields if field not in record]
    if missing:
        return f"missing required field(s): {', '.join(sorted(missing))}"
    if required_fields is V2_REQUIRED_RECORD_FIELDS:
        sensitive_additions = sorted(
            set(record) & SENSITIVE_ADDITIVE_RECORD_FIELDS
        )
        if sensitive_additions:
            return (
                "unknown safety-sensitive field(s): "
                + ", ".join(sensitive_additions)
            )
    for field, expected_type in required_fields.items():
        if not matches_type(record[field], expected_type):
            return (
                f"field {field!r} must be {expected_type}, "
                f"got {type_name(record[field])}"
            )
    for field, expected_type in optional_fields.items():
        if field in record and not matches_type(record[field], expected_type):
            return (
                f"optional field {field!r} must be {expected_type}, "
                f"got {type_name(record[field])}"
            )
    return None


def complete_v2_result(api_url, jobs, raw_record_count, warnings=()):
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_message=f"Fetched complete Alignerr v2 snapshot: {api_url}",
        source_type="alignerr-marketplace",
        outcome=ProviderOutcome.SUCCESS,
        snapshot_complete=True,
        pagination_complete=True,
        empty_snapshot_validated=False,
        payload_shape=V2_PAYLOAD_SHAPE,
        raw_record_count=raw_record_count,
        normalized_record_count=len(jobs),
        rejected_record_count=0,
        warnings=tuple(warnings),
        schema_fingerprint=schema_fingerprint("v2"),
    )


def partial_v2_result(
    api_url,
    jobs,
    raw_record_count,
    rejected_record_count,
    warnings,
):
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_message=f"Alignerr v2 snapshot was incomplete: {api_url}",
        source_type="alignerr-marketplace",
        outcome=ProviderOutcome.PARTIAL,
        snapshot_complete=False,
        pagination_complete=False,
        payload_shape=V2_PAYLOAD_SHAPE,
        raw_record_count=raw_record_count,
        normalized_record_count=len(jobs),
        rejected_record_count=rejected_record_count,
        warnings=tuple(warnings),
        schema_fingerprint=schema_fingerprint("v2"),
    )


def contract_drift_result(api_url, message, payload_shape):
    return CompanyCrawlResult(
        jobs=[],
        used_sample_data=False,
        source_message=f"Alignerr contract drift at {api_url}: {message}",
        source_type="alignerr-marketplace",
        outcome=ProviderOutcome.CONTRACT_DRIFT,
        snapshot_complete=False,
        pagination_complete=False,
        payload_shape=payload_shape,
        raw_record_count=0,
        normalized_record_count=0,
        rejected_record_count=0,
        warnings=(message,),
        schema_fingerprint="",
    )


def schema_fingerprint(version):
    if version == "v1":
        descriptor = {
            "optional_record_fields": V1_OPTIONAL_RECORD_FIELDS,
            "root": "array",
            "required_record_fields": V1_REQUIRED_RECORD_FIELDS,
            "supported_version": "alignerr-v1",
        }
    elif version == "v2":
        descriptor = {
            "optional_record_fields": V2_OPTIONAL_RECORD_FIELDS,
            "pagination_fields": V2_TOP_LEVEL_FIELDS,
            "required_record_fields": V2_REQUIRED_RECORD_FIELDS,
            "root": "object",
            "supported_version": "alignerr-v2",
        }
    else:
        raise ValueError(f"Unknown Alignerr schema version: {version}")
    encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"alignerr-{version}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def add_pagination(api_url, limit, offset):
    parsed = urlparse(api_url)
    query = parse_qs(parsed.query)
    query["limit"] = [str(limit)]
    query["offset"] = [str(offset)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def normalize_application_url(raw_url, expected_job_id=None):
    raw_url = clean_value(raw_url)
    if not raw_url:
        return None
    raw_parsed = urlparse(raw_url)
    if not raw_parsed.scheme and not raw_url.startswith("/jobs/"):
        return None
    url = urljoin(ALIGNERR_ORIGIN, raw_url)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if not parsed.path or parsed.path == "/":
        return None
    if (
        expected_job_id
        and parsed.netloc.lower() in {"alignerr.com", "www.alignerr.com"}
        and parsed.path.startswith("/jobs/")
        and parsed.path.rstrip("/").split("/")[-1] != expected_job_id
    ):
        return None
    return url


def page_signature(records):
    return tuple(
        clean_value(record.get("id")) if isinstance(record, dict) else None
        for record in records
    )


def additive_field_warnings(payload):
    warnings = []
    top_level = sorted(set(payload) - set(V2_TOP_LEVEL_FIELDS))
    if top_level:
        warnings.append(
            "Observed additive v2 envelope field(s): " + ", ".join(top_level)
        )
    known_record_fields = set(V2_REQUIRED_RECORD_FIELDS) | set(V2_OPTIONAL_RECORD_FIELDS)
    record_fields = set()
    for record in payload.get("jobs") or []:
        if isinstance(record, dict):
            record_fields.update(set(record) - known_record_fields)
    if record_fields:
        warnings.append(
            "Observed additive v2 record field(s): "
            + ", ".join(sorted(record_fields))
        )
    return warnings


def describe_root_shape(payload):
    if isinstance(payload, dict):
        return f"object:keys={','.join(sorted(str(key) for key in payload))}"
    return f"root:{type_name(payload)}"


def matches_type(value, expected_type):
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "string|null":
        return value is None or isinstance(value, str)
    raise ValueError(f"Unsupported contract type: {expected_type}")


def is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def clean_value(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None

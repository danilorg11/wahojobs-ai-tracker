import json
from hashlib import sha256
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from wahojobs.crawler.types import CompanyCrawlResult, JobCandidate, ProviderOutcome
from wahojobs.crawler.source_content import first_text, nonempty_metadata, selected_metadata


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://www.micro1.ai",
    "Referer": "https://www.micro1.ai/experts/opportunities",
}

REQUEST_BODY = {
    "action": "get_all_jobs",
    "filters": {
        "type": ["EXPERT"],
    },
}

PAYLOAD_SHAPE = "micro1-job-portal:v1:paginated-object"
SOURCE_TYPE = "micro1-marketplace"
PAGE_LIMIT = 100


def fetch_micro1_jobs(api_url):
    return list(fetch_micro1_snapshot(api_url).jobs)


def fetch_micro1_snapshot(api_url):
    jobs = []
    raw_records = []
    page_payloads = []
    seen_external_ids = set()
    total = None
    page = 1
    rejected_count = 0

    while total is None or len(raw_records) < total:
        data = fetch_page(api_url, page, PAGE_LIMIT)
        page_payloads.append(data)
        page_total = validated_total(data)
        if total is None:
            total = page_total
        elif page_total != total:
            return build_snapshot_result(
                api_url,
                jobs,
                raw_records,
                page_payloads,
                rejected_count,
                outcome=ProviderOutcome.CONTRACT_DRIFT,
                warning=(
                    "micro1 reported a changing total across pages; "
                    "the snapshot was not applied."
                ),
            )

        page_jobs = data.get("data") or []
        if not isinstance(page_jobs, list):
            raise ValueError("micro1 response data was not a job list.")
        raw_records.extend(page_jobs)

        for job in page_jobs:
            if not should_include_job(job):
                rejected_count += 1
                continue
            candidate = parse_micro1_job(job)
            if candidate.external_id in seen_external_ids:
                return build_snapshot_result(
                    api_url,
                    jobs,
                    raw_records,
                    page_payloads,
                    rejected_count,
                    outcome=ProviderOutcome.CONTRACT_DRIFT,
                    warning=(
                        "micro1 returned a duplicate job identifier; "
                        "the snapshot was not applied."
                    ),
                )
            seen_external_ids.add(candidate.external_id)
            jobs.append(candidate)

        if len(raw_records) > total:
            return build_snapshot_result(
                api_url,
                jobs,
                raw_records,
                page_payloads,
                rejected_count,
                outcome=ProviderOutcome.CONTRACT_DRIFT,
                warning=(
                    "micro1 returned more records than its reported total; "
                    "the snapshot was not applied."
                ),
            )

        if len(raw_records) == total:
            break

        if not page_jobs:
            return build_snapshot_result(
                api_url,
                jobs,
                raw_records,
                page_payloads,
                rejected_count,
                outcome=ProviderOutcome.PARTIAL,
                warning=(
                    "micro1 pagination ended before the reported total; "
                    "removals were skipped."
                ),
            )
        page += 1

    if rejected_count:
        return build_snapshot_result(
            api_url,
            jobs,
            raw_records,
            page_payloads,
            rejected_count,
            outcome=ProviderOutcome.CONTRACT_DRIFT,
            warning=(
                "micro1 returned records missing required job fields; "
                "the snapshot was not applied."
            ),
        )

    return build_snapshot_result(
        api_url,
        jobs,
        raw_records,
        page_payloads,
        rejected_count,
        outcome=ProviderOutcome.SUCCESS,
        snapshot_complete=True,
        pagination_complete=True,
        empty_snapshot_validated=total == 0,
    )


def validated_total(data):
    total = data.get("total")
    if isinstance(total, bool):
        raise ValueError("micro1 response total was not a non-negative integer.")
    try:
        total = int(total)
    except (TypeError, ValueError):
        raise ValueError(
            "micro1 response total was not a non-negative integer."
        ) from None
    if total < 0:
        raise ValueError("micro1 response total was not a non-negative integer.")
    return total


def build_snapshot_result(
    api_url,
    jobs,
    raw_records,
    page_payloads,
    rejected_count,
    *,
    outcome,
    snapshot_complete=False,
    pagination_complete=False,
    empty_snapshot_validated=False,
    warning=None,
):
    return CompanyCrawlResult(
        jobs=list(jobs),
        used_sample_data=False,
        source_type=SOURCE_TYPE,
        source_message=(
            f"Fetched live micro1 expert opportunities from API: {api_url}"
        ),
        outcome=outcome,
        snapshot_complete=snapshot_complete,
        pagination_complete=pagination_complete,
        empty_snapshot_validated=empty_snapshot_validated,
        payload_shape=PAYLOAD_SHAPE,
        raw_record_count=len(raw_records),
        normalized_record_count=len(jobs),
        rejected_record_count=rejected_count,
        warnings=(warning,) if warning else (),
        schema_fingerprint=micro1_schema_fingerprint(page_payloads, raw_records),
    )


def micro1_schema_fingerprint(page_payloads, raw_records):
    shape = {
        "top_level_keys": sorted(
            {key for payload in page_payloads for key in payload.keys()}
        ),
        "record_keys": sorted(
            {
                key
                for record in raw_records
                if isinstance(record, dict)
                for key in record.keys()
            }
        ),
    }
    digest = sha256(
        json.dumps(shape, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"micro1-job-portal-v1:sha256:{digest}"


def fetch_page(api_url, page, limit):
    query = urlencode({"page": page, "limit": limit, "keyword": ""})
    separator = "&" if "?" in api_url else "?"
    url = f"{api_url}{separator}{query}"
    body = json.dumps(REQUEST_BODY).encode("utf-8")
    request = Request(url, data=body, headers=REQUEST_HEADERS, method="POST")

    with urlopen(request, timeout=60) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")

    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("micro1 response was not a JSON object.")
    if data.get("status") is not True:
        raise ValueError(f"micro1 response was not successful: {data.get('message')}")
    return data


def should_include_job(job):
    return (
        isinstance(job, dict)
        and bool(clean_value(job.get("job_id")))
        and bool(clean_value(job.get("job_name")))
        and bool(clean_value(job.get("apply_url")))
    )


def parse_micro1_job(job):
    domain = clean_value(job.get("domain_slug"))
    role_type = clean_value(job.get("role_type"))
    category = domain or role_type or fallback_category(job) or "Unknown"
    source_body = first_text(
        job,
        ("job_description", "description", "description_text", "jobDescription"),
    )

    return JobCandidate(
        external_id=clean_value(job.get("job_id")),
        title=clean_value(job.get("job_name")),
        location=clean_value(job.get("location_type")) or "Remote",
        url=clean_value(job.get("apply_url")),
        department=category,
        expertise=category,
        commitment=clean_value(job.get("engagement_type")),
        source_body=source_body,
        source_body_format="text/plain" if source_body else None,
        source_metadata=nonempty_metadata(
            selected_metadata(
                job,
                (
                    "skills",
                    "job_tags",
                    "role_type",
                    "domain_slug",
                    "location_type",
                    "engagement_type",
                    "responsibilities",
                    "requirements",
                    "qualifications",
                ),
            )
        ),
        source_updated_at=clean_value(
            job.get("updated_at") or job.get("updatedAt")
        ),
    )


def clean_value(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None


def fallback_category(job):
    haystack = build_fallback_haystack(job)
    rules = (
        (
            "Language / Linguistics",
            (
                "language",
                "bilingual",
                "translation",
                "linguistic",
                "portuguese",
                "swedish",
                "czech",
                "khmer",
                "romanian",
                "english language expert",
            ),
        ),
        (
            "Audio / Speech",
            (
                "audio",
                "voice",
                "dubbing",
                "voice over",
            ),
        ),
        (
            "Data Collection",
            (
                "video capture",
                "household data",
                "data collection",
                "sensor data capture",
            ),
        ),
        (
            "Coding / Software Evaluation",
            (
                "software",
                "backend",
                "python",
                "javascript",
                "typescript",
                "go",
                "java",
                "c#",
                "ai quality",
                "testing",
            ),
        ),
        (
            "Data Annotation",
            (
                "quality analyst",
                "video qc",
                "quality control",
                "annotation",
            ),
        ),
        (
            "Data Operations",
            (
                "project management",
                "data operations",
                "human data manager",
            ),
        ),
        (
            "Technical Support / IT",
            (
                "network administration",
                "systems administrator",
                "technical support",
                "support engineer",
            ),
        ),
    )

    for category, keywords in rules:
        if any(keyword in haystack for keyword in keywords):
            return category
    return None


def build_fallback_haystack(job):
    parts = [clean_value(job.get("job_name")) or ""]
    skills = job.get("skills") or []
    if isinstance(skills, list):
        parts.extend(clean_value(skill) or "" for skill in skills)
    tags = job.get("job_tags") or []
    if isinstance(tags, list):
        parts.extend(clean_value(tag) or "" for tag in tags)
    return " ".join(parts).lower()

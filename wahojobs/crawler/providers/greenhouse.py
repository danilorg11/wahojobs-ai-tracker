from dataclasses import dataclass
import hashlib
import json
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from wahojobs.crawler.types import (
    CompanyCrawlResult,
    JobCandidate,
    ProviderOutcome,
)


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json",
}
REQUEST_TIMEOUT_SECONDS = 60
MAX_RECORDS = 20_000
MAX_RAW_RECORD_BYTES = 2_000_000
GREENHOUSE_V1_PAYLOAD_SHAPE = "greenhouse-job-board-v1:jobs+department-tree"
APPLICATION_ERROR_FIELDS = {"error", "errors", "status", "success"}

JOBS_ROOT_SCHEMA = {
    "jobs": "list",
    "meta": "object",
    "meta.total": "integer",
}
JOB_RECORD_SCHEMA = {
    "absolute_url": "string",
    "departments": "list[department]",
    "id": "integer",
    "location": "object{name:string}",
    "offices": "list[office]",
    "title": "string",
    "updated_at": "string",
}
JOB_OPTIONAL_PUBLIC_SCHEMA = {
    "application_deadline": "string|null",
    "application_url": "string|null",
    "company_name": "string|null",
    "content": "string|null",
    "data_compliance": "list|null",
    "education": "string|list|object|null",
    "first_published": "string|null",
    "internal_job_id": "integer|null",
    "language": "string|null",
    "metadata": "list|null",
    "requisition_id": "string|null",
    "unknown_optional_fields": "bounded deterministic JSON",
}
JOB_DEPARTMENT_SCHEMA = {
    "child_ids": "list[integer]",
    "id": "integer",
    "name": "string",
    "parent_id": "integer|null",
}
JOB_OFFICE_SCHEMA = {
    "child_ids": "list[integer]",
    "id": "integer",
    "location": "string|null",
    "name": "string",
    "parent_id": "integer|null",
}
DEPARTMENT_NODE_SCHEMA = {
    "children": "list[department-node]",
    "id": "integer",
    "jobs": "list[job-reference]",
    "name": "string",
}
DEPARTMENT_JOB_SCHEMA = {
    "absolute_url": "string",
    "id": "integer",
    "location": "object{name:string}",
    "title": "string",
}


@dataclass(frozen=True)
class GreenhouseBoardConfig:
    source_name: str
    board_token: str
    company_id: str = ""
    allowed_job_hosts: tuple[str, ...] = (
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    )
    api_host: str = "https://boards-api.greenhouse.io"
    root_department_id: int | None = None
    include_content: bool = True
    max_records: int = MAX_RECORDS


@dataclass(frozen=True)
class GreenhouseDepartmentMetadata:
    department_id: int
    name: str
    parent_id: int | None
    child_ids: tuple[int, ...]


@dataclass(frozen=True)
class GreenhouseOfficeMetadata:
    office_id: int
    name: str
    location: str | None
    parent_id: int | None
    child_ids: tuple[int, ...]


@dataclass(frozen=True)
class GreenhouseSourceRecord:
    source_name: str
    company_id: str
    board_token: str
    greenhouse_job_id: int
    external_id: str
    title: str
    url: str
    application_url: str | None
    location: str
    additional_locations: tuple[str, ...]
    description_html: str | None
    updated_at: str
    internal_job_id: int | None
    requisition_id: str | None
    first_published: str | None
    application_deadline: str | None
    language: str | None
    company_name: str | None
    metadata_json: str
    education_json: str
    compliance_json: str
    compensation_json: str
    raw_public_payload_json: str
    departments: tuple[GreenhouseDepartmentMetadata, ...]
    offices: tuple[GreenhouseOfficeMetadata, ...]


@dataclass(frozen=True)
class ParsedInventoryJob:
    candidate: JobCandidate
    source_record: GreenhouseSourceRecord
    department_ids: tuple[int, ...]
    department_names: tuple[str, ...]
    title: str
    location: str
    url: str


@dataclass(frozen=True)
class DepartmentTree:
    department_paths: dict[int, str]
    job_paths: dict[str, tuple[str, ...]]
    job_references: dict[str, tuple[dict, ...]]
    node_count: int


def fetch_greenhouse_snapshot(config: GreenhouseBoardConfig, configured_url=None):
    """Fetch a strict full Greenhouse board snapshot.

    The jobs endpoint is the authoritative inventory. The configured department
    tree is required Meridial enrichment and cross-endpoint validation, but it is
    never used as a substitute inventory.
    """
    jobs_url = build_jobs_url(config)
    jobs_payload = request_json(jobs_url)
    root_error = validate_jobs_root(jobs_payload)
    if root_error:
        return contract_drift_result(config, root_error, configured_url)

    raw_records = jobs_payload["jobs"]
    raw_record_count = len(raw_records)
    if raw_record_count > config.max_records:
        return partial_result(
            config,
            [],
            raw_record_count,
            0,
            [
                f"Greenhouse inventory contains {raw_record_count} records, exceeding "
                f"the safety bound of {config.max_records}."
            ],
            configured_url,
        )
    if jobs_payload["meta"]["total"] != raw_record_count:
        return partial_result(
            config,
            [],
            raw_record_count,
            0,
            [
                "Greenhouse meta.total does not match the returned full jobs array: "
                f"{jobs_payload['meta']['total']} != {raw_record_count}."
            ],
            configured_url,
        )

    parsed_by_id = {}
    warnings = additive_field_warnings(jobs_payload)
    rejected_record_count = 0
    duplicate_ids = set()
    for index, record in enumerate(raw_records):
        parsed, error = parse_inventory_record(record, config)
        if error:
            rejected_record_count += 1
            warnings.append(f"Rejected inventory record at index {index}: {error}")
            continue
        external_id = parsed.candidate.external_id
        if external_id in parsed_by_id:
            rejected_record_count += 1
            duplicate_ids.add(external_id)
            conflict = parsed != parsed_by_id[external_id]
            detail = "conflicting duplicate" if conflict else "duplicate"
            warnings.append(f"Rejected {detail} Greenhouse job id {external_id}.")
            continue
        parsed_by_id[external_id] = parsed

    tree = None
    if config.root_department_id is not None:
        tree_payload = request_json(build_department_tree_url(config))
        tree_root_error = validate_department_tree_root(tree_payload)
        if tree_root_error:
            return contract_drift_result(
                config,
                tree_root_error,
                configured_url,
                raw_record_count=raw_record_count,
            )
        tree, tree_errors = parse_department_tree(
            tree_payload,
            expected_root_id=config.root_department_id,
            config=config,
        )
        if tree_errors:
            warnings.extend(tree_errors)
            return partial_result(
                config,
                [item.candidate for item in parsed_by_id.values()],
                raw_record_count,
                rejected_record_count,
                warnings,
                configured_url,
                source_records=source_records_from(parsed_by_id),
            )

        consistency_errors, consistency_warnings = validate_cross_endpoint_consistency(
            parsed_by_id,
            tree,
        )
        warnings.extend(consistency_warnings)
        if consistency_errors:
            warnings.extend(consistency_errors)
            return partial_result(
                config,
                enrich_candidates(parsed_by_id, tree),
                raw_record_count,
                rejected_record_count,
                warnings,
                configured_url,
                source_records=source_records_from(parsed_by_id),
            )

    jobs = enrich_candidates(parsed_by_id, tree)
    if duplicate_ids or rejected_record_count:
        return partial_result(
            config,
            jobs,
            raw_record_count,
            rejected_record_count,
            warnings,
            configured_url,
            source_records=source_records_from(parsed_by_id),
        )
    if not jobs:
        warnings.append(
            "Greenhouse returned a valid empty jobs inventory; empty snapshots require "
            "a separately reviewed source policy and cannot authorize removals."
        )
        return partial_result(
            config,
            jobs,
            raw_record_count,
            rejected_record_count,
            warnings,
            configured_url,
            source_records=source_records_from(parsed_by_id),
        )

    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="greenhouse-job-board-v1",
        source_message=source_message(config, configured_url, "complete snapshot"),
        outcome=ProviderOutcome.SUCCESS,
        snapshot_complete=True,
        pagination_complete=True,
        empty_snapshot_validated=False,
        payload_shape=GREENHOUSE_V1_PAYLOAD_SHAPE,
        raw_record_count=raw_record_count,
        normalized_record_count=len(jobs),
        rejected_record_count=0,
        warnings=tuple(warnings),
        schema_fingerprint=greenhouse_schema_fingerprint(),
        source_records=source_records_from(parsed_by_id),
    )


def request_json(url):
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")
    return json.loads(payload)


def build_jobs_url(config):
    query = urlencode({"content": "true"}) if config.include_content else ""
    base = f"{config.api_host.rstrip('/')}/v1/boards/{config.board_token}/jobs"
    return f"{base}?{query}" if query else base


def build_department_tree_url(config):
    return (
        f"{config.api_host.rstrip('/')}/v1/boards/{config.board_token}/departments/"
        f"{config.root_department_id}?{urlencode({'render_as': 'tree'})}"
    )


def validate_jobs_root(payload):
    if not isinstance(payload, dict):
        return f"Unknown Greenhouse jobs root type: {type_name(payload)}."
    if APPLICATION_ERROR_FIELDS.intersection(payload):
        fields = sorted(APPLICATION_ERROR_FIELDS.intersection(payload))
        return f"Greenhouse jobs response contains application status fields: {fields}."
    if "jobs" not in payload or "meta" not in payload:
        return "Greenhouse jobs response is missing required jobs/meta keys."
    if not isinstance(payload["jobs"], list):
        return "Greenhouse jobs response field 'jobs' is not a list."
    if not isinstance(payload["meta"], dict):
        return "Greenhouse jobs response field 'meta' is not an object."
    total = payload["meta"].get("total")
    if not is_integer(total) or total < 0:
        return "Greenhouse jobs response field 'meta.total' is not a non-negative integer."
    return None


def validate_department_tree_root(payload):
    if not isinstance(payload, dict):
        return f"Unknown Greenhouse department-tree root type: {type_name(payload)}."
    if APPLICATION_ERROR_FIELDS.intersection(payload):
        fields = sorted(APPLICATION_ERROR_FIELDS.intersection(payload))
        return f"Greenhouse department response contains application status fields: {fields}."
    missing = [field for field in DEPARTMENT_NODE_SCHEMA if field not in payload]
    if missing:
        return f"Greenhouse department-tree root is missing required keys: {missing}."
    return None


def parse_inventory_record(record, config):
    if not isinstance(record, dict):
        return None, f"record is {type_name(record)}, expected object."

    required = ("id", "title", "absolute_url", "location", "departments", "offices", "updated_at")
    missing = [field for field in required if field not in record]
    if missing:
        return None, f"missing required fields {missing}."

    job_id = record["id"]
    if not is_integer(job_id) or job_id < 1:
        return None, "id must be a positive integer."
    title = strict_text(record["title"])
    if not title:
        return None, "title must be a non-empty string."
    url = strict_text(record["absolute_url"])
    url_error = validate_job_url(url, job_id, config)
    if url_error:
        return None, url_error
    location, location_error = parse_location(record["location"])
    if location_error:
        return None, location_error
    updated_at = strict_text(record["updated_at"])
    if not updated_at:
        return None, "updated_at must be a non-empty string."

    departments, department_error = parse_job_departments(record["departments"])
    if department_error:
        return None, department_error
    offices, office_error = parse_job_offices(record["offices"])
    if office_error:
        return None, office_error
    optional, optional_error = parse_optional_public_metadata(record)
    if optional_error:
        return None, optional_error

    department_names = tuple(item.name for item in departments)
    department = ", ".join(department_names) or None
    candidate = JobCandidate(
        external_id=str(job_id),
        title=title,
        location=location,
        url=url,
        department=department,
        expertise=extract_expertise(department),
        commitment=None,
    )
    return (
        ParsedInventoryJob(
            candidate=candidate,
            source_record=GreenhouseSourceRecord(
                source_name=config.source_name,
                company_id=config.company_id,
                board_token=config.board_token,
                greenhouse_job_id=job_id,
                external_id=str(job_id),
                title=title,
                url=url,
                application_url=optional["application_url"],
                location=location,
                additional_locations=additional_locations(location, offices),
                description_html=strict_text(record.get("content")),
                updated_at=updated_at,
                internal_job_id=optional["internal_job_id"],
                requisition_id=optional["requisition_id"],
                first_published=optional["first_published"],
                application_deadline=optional["application_deadline"],
                language=optional["language"],
                company_name=optional["company_name"],
                metadata_json=optional["metadata_json"],
                education_json=optional["education_json"],
                compliance_json=optional["compliance_json"],
                compensation_json=optional["compensation_json"],
                raw_public_payload_json=optional["raw_public_payload_json"],
                departments=tuple(departments),
                offices=tuple(offices),
            ),
            department_ids=tuple(item.department_id for item in departments),
            department_names=department_names,
            title=title,
            location=location,
            url=url,
        ),
        None,
    )


def parse_optional_public_metadata(record):
    validators = {
        "company_name": (str, type(None)),
        "content": (str, type(None)),
        "internal_job_id": (int, type(None)),
        "requisition_id": (str, type(None)),
        "first_published": (str, type(None)),
        "application_deadline": (str, type(None)),
        "language": (str, type(None)),
        "metadata": (list, type(None)),
        "education": (str, list, dict, type(None)),
        "data_compliance": (list, type(None)),
        "application_url": (str, type(None)),
        "include_ai_disclaimer": (bool, type(None)),
        "ai_disclaimer": (str, type(None)),
        "ai_opt_out_request_url": (str, type(None)),
    }
    for field, allowed_types in validators.items():
        if field not in record:
            continue
        value = record[field]
        if not isinstance(value, allowed_types) or (
            field == "internal_job_id" and isinstance(value, bool)
        ):
            return None, f"{field} has unsupported type {type_name(value)}."

    application_url = strict_text(record.get("application_url"))
    if application_url and application_url != record.get("absolute_url"):
        application_error = validate_public_https_url(application_url)
        if application_error:
            return None, f"application_url {application_error}"
    else:
        application_url = None

    compensation = {
        key: value
        for key, value in record.items()
        if any(marker in key.casefold() for marker in ("pay", "compensation", "salary"))
    }
    try:
        raw_payload = deterministic_json(record)
        metadata_json = deterministic_json(record.get("metadata"))
        education_json = deterministic_json(record.get("education"))
        compliance_json = deterministic_json(record.get("data_compliance"))
        compensation_json = deterministic_json(compensation)
    except (TypeError, ValueError) as exc:
        return None, f"public metadata is not JSON serializable: {exc}."
    if len(raw_payload.encode("utf-8")) > MAX_RAW_RECORD_BYTES:
        return None, "public metadata exceeds the bounded raw-record size."

    internal_job_id = record.get("internal_job_id")
    if internal_job_id is not None and internal_job_id < 1:
        return None, "internal_job_id must be a positive integer or null."
    return {
        "application_url": application_url,
        "internal_job_id": internal_job_id,
        "requisition_id": strict_text(record.get("requisition_id")),
        "first_published": strict_text(record.get("first_published")),
        "application_deadline": strict_text(record.get("application_deadline")),
        "language": strict_text(record.get("language")),
        "company_name": strict_text(record.get("company_name")),
        "metadata_json": metadata_json,
        "education_json": education_json,
        "compliance_json": compliance_json,
        "compensation_json": compensation_json,
        "raw_public_payload_json": raw_payload,
    }, None


def additional_locations(primary_location, offices):
    values = []
    for office in offices:
        for value in (office.name, office.location):
            cleaned = clean_value(value)
            if cleaned and cleaned != primary_location and cleaned not in values:
                values.append(cleaned)
    return tuple(values)


def validate_public_https_url(value):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return "must be a valid HTTPS URL."
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return "must be a valid HTTPS URL."
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return "must be a valid HTTPS URL."
    return None


def deterministic_json(value):
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_location(value):
    if not isinstance(value, dict):
        return None, "location must be an object."
    name = strict_text(value.get("name"))
    if not name:
        return None, "location.name must be a non-empty string."
    return name, None


def parse_job_departments(value):
    if not isinstance(value, list):
        return None, "departments must be a list."
    parsed = []
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            return None, f"departments[{index}] must be an object."
        missing = [field for field in JOB_DEPARTMENT_SCHEMA if field not in item]
        if missing:
            return None, f"departments[{index}] is missing fields {missing}."
        department_id = item["id"]
        name = strict_text(item["name"])
        parent_id = item["parent_id"]
        child_ids = item["child_ids"]
        if not is_integer(department_id) or department_id < 0:
            return None, f"departments[{index}].id must be a non-negative integer."
        if not name:
            return None, f"departments[{index}].name must be a non-empty string."
        if parent_id is not None and not is_integer(parent_id):
            return None, f"departments[{index}].parent_id must be an integer or null."
        if not isinstance(child_ids, list) or not all(is_integer(value) for value in child_ids):
            return None, f"departments[{index}].child_ids must be a list of integers."
        if department_id in seen:
            return None, f"departments contains duplicate id {department_id}."
        seen.add(department_id)
        parsed.append(
            GreenhouseDepartmentMetadata(
                department_id=department_id,
                name=name,
                parent_id=parent_id,
                child_ids=tuple(child_ids),
            )
        )
    return parsed, None


def parse_job_offices(value):
    if not isinstance(value, list):
        return None, "offices must be a list."
    parsed = []
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            return None, f"offices[{index}] must be an object."
        missing = [field for field in JOB_OFFICE_SCHEMA if field not in item]
        if missing:
            return None, f"offices[{index}] is missing fields {missing}."
        office_id = item["id"]
        name = strict_text(item["name"])
        parent_id = item["parent_id"]
        location = item["location"]
        child_ids = item["child_ids"]
        if not is_integer(office_id) or office_id < 0:
            return None, f"offices[{index}].id must be a non-negative integer."
        if office_id in seen:
            return None, f"offices contains duplicate id {office_id}."
        if not name:
            return None, f"offices[{index}].name must be a non-empty string."
        if parent_id is not None and not is_integer(parent_id):
            return None, f"offices[{index}].parent_id must be an integer or null."
        if location is not None and not isinstance(location, str):
            return None, f"offices[{index}].location must be a string or null."
        if not isinstance(child_ids, list) or not all(is_integer(value) for value in child_ids):
            return None, f"offices[{index}].child_ids must be a list of integers."
        seen.add(office_id)
        parsed.append(
            GreenhouseOfficeMetadata(
                office_id=office_id,
                name=name,
                location=clean_value(location),
                parent_id=parent_id,
                child_ids=tuple(child_ids),
            )
        )
    return parsed, None


def validate_job_offices(value):
    _parsed, error = parse_job_offices(value)
    return error


def parse_department_tree(root, expected_root_id, config):
    department_paths = {}
    job_paths = {}
    job_references = {}
    errors = []
    node_count = 0

    if root.get("id") != expected_root_id:
        errors.append(
            f"Department-tree root id {root.get('id')!r} does not match configured "
            f"root {expected_root_id}."
        )

    def walk(node, path, ancestry):
        nonlocal node_count
        node_count += 1
        if not isinstance(node, dict):
            errors.append(f"Department node at {' > '.join(path) or '<root>'} is not an object.")
            return
        missing = [field for field in DEPARTMENT_NODE_SCHEMA if field not in node]
        if missing:
            errors.append(f"Department node is missing required fields {missing}.")
            return
        department_id = node["id"]
        name = strict_text(node["name"])
        if not is_integer(department_id) or department_id < 0:
            errors.append(f"Department id {department_id!r} is not a non-negative integer.")
            return
        if not name:
            errors.append(f"Department {department_id} has an invalid name.")
            return
        if department_id in ancestry:
            errors.append(f"Department hierarchy cycle detected at id {department_id}.")
            return
        current_path = (*path, name)
        path_text = " > ".join(current_path)
        existing_path = department_paths.get(department_id)
        if existing_path is not None and existing_path != path_text:
            errors.append(
                f"Department id {department_id} appears at conflicting paths "
                f"{existing_path!r} and {path_text!r}."
            )
            return
        department_paths[department_id] = path_text

        jobs = node["jobs"]
        children = node["children"]
        if not isinstance(jobs, list):
            errors.append(f"Department {department_id} jobs must be a list.")
            return
        if not isinstance(children, list):
            errors.append(f"Department {department_id} children must be a list.")
            return
        seen_here = set()
        for index, reference in enumerate(jobs):
            parsed, error = parse_department_job_reference(reference, config)
            if error:
                errors.append(
                    f"Department {department_id} job reference {index}: {error}"
                )
                continue
            job_id = parsed["id"]
            if job_id in seen_here:
                errors.append(
                    f"Department {department_id} contains duplicate job id {job_id}."
                )
                continue
            seen_here.add(job_id)
            job_paths.setdefault(job_id, []).append(path_text)
            job_references.setdefault(job_id, []).append(parsed)
        for child in children:
            walk(child, current_path, {*ancestry, department_id})

    walk(root, (), set())
    return (
        DepartmentTree(
            department_paths=department_paths,
            job_paths={key: tuple(values) for key, values in job_paths.items()},
            job_references={key: tuple(values) for key, values in job_references.items()},
            node_count=node_count,
        ),
        errors,
    )


def parse_department_job_reference(record, config):
    if not isinstance(record, dict):
        return None, "record must be an object."
    missing = [field for field in DEPARTMENT_JOB_SCHEMA if field not in record]
    if missing:
        return None, f"missing required fields {missing}."
    job_id = record["id"]
    if not is_integer(job_id) or job_id < 1:
        return None, "id must be a positive integer."
    title = strict_text(record["title"])
    if not title:
        return None, "title must be a non-empty string."
    url = strict_text(record["absolute_url"])
    url_error = validate_job_url(url, job_id, config)
    if url_error:
        return None, url_error
    location, location_error = parse_location(record["location"])
    if location_error:
        return None, location_error
    return {
        "id": str(job_id),
        "title": title,
        "url": url,
        "location": location,
    }, None


def validate_cross_endpoint_consistency(parsed_by_id, tree):
    errors = []
    warnings = []
    inventory_ids = set(parsed_by_id)
    tree_ids = set(tree.job_references)
    for job_id in sorted(tree_ids - inventory_ids):
        errors.append(
            f"Department enrichment references unknown inventory job id {job_id}."
        )
    for job_id in sorted(tree_ids & inventory_ids):
        inventory = parsed_by_id[job_id]
        for reference in tree.job_references[job_id]:
            for field in ("title", "location", "url"):
                if getattr(inventory, field) != reference[field]:
                    errors.append(
                        f"Greenhouse job id {job_id} has conflicting {field} values "
                        "between inventory and department enrichment."
                    )
    for job_id in sorted(inventory_ids - tree_ids):
        inventory = parsed_by_id[job_id]
        if inventory.department_ids:
            warnings.append(
                f"Inventory job id {job_id} is absent from department job enrichment; "
                "structured inventory departments were retained."
            )
        else:
            warnings.append(
                f"Inventory job id {job_id} is unassigned; explicit empty department "
                "metadata was retained."
            )
    return errors, warnings


def enrich_candidates(parsed_by_id, tree):
    jobs = []
    for job_id, parsed in parsed_by_id.items():
        paths = []
        if tree is not None:
            for department_id in parsed.department_ids:
                path = tree.department_paths.get(department_id)
                if path and path not in paths:
                    paths.append(path)
            for path in tree.job_paths.get(job_id, ()):
                if path not in paths:
                    paths.append(path)
        department = " | ".join(paths) or None
        if department is None and parsed.department_names:
            department = ", ".join(parsed.department_names)
        jobs.append(
            JobCandidate(
                external_id=parsed.candidate.external_id,
                title=parsed.title,
                location=parsed.location,
                url=parsed.url,
                department=department,
                expertise=extract_expertise(department),
                commitment=None,
            )
        )
    return jobs


def source_records_from(parsed_by_id):
    return tuple(item.source_record for item in parsed_by_id.values())


def validate_job_url(url, job_id, config):
    if not url:
        return "absolute_url must be a non-empty string."
    if not isinstance(url, str) or any(ord(char) < 32 for char in url):
        return "absolute_url contains invalid characters."
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return "absolute_url is malformed."
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        return "absolute_url must be an absolute HTTPS URL."
    if (parsed.hostname or "").casefold() not in set(config.allowed_job_hosts):
        return "absolute_url host is not approved for this registry board."
    normalized_path = parsed.path.rstrip("/")
    expected_path = f"/{config.board_token}/jobs/{job_id}"
    if normalized_path != expected_path:
        return (
            "absolute_url must be scoped to the configured board and exact stable "
            f"Greenhouse job id ({expected_path})."
        )
    return None


def partial_result(
    config,
    jobs,
    raw_record_count,
    rejected_record_count,
    warnings,
    configured_url,
    *,
    source_records=(),
):
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="greenhouse-job-board-v1",
        source_message=source_message(config, configured_url, "incomplete snapshot"),
        outcome=ProviderOutcome.PARTIAL,
        snapshot_complete=False,
        pagination_complete=False,
        empty_snapshot_validated=False,
        payload_shape=GREENHOUSE_V1_PAYLOAD_SHAPE,
        raw_record_count=raw_record_count,
        normalized_record_count=len(jobs),
        rejected_record_count=rejected_record_count,
        warnings=tuple(warnings),
        schema_fingerprint=greenhouse_schema_fingerprint(),
        source_records=tuple(source_records),
    )


def contract_drift_result(
    config,
    error,
    configured_url,
    *,
    raw_record_count=0,
):
    return CompanyCrawlResult(
        jobs=[],
        used_sample_data=False,
        source_type="greenhouse-job-board-v1",
        source_message=source_message(config, configured_url, f"contract drift: {error}"),
        outcome=ProviderOutcome.CONTRACT_DRIFT,
        snapshot_complete=False,
        pagination_complete=False,
        empty_snapshot_validated=False,
        payload_shape=GREENHOUSE_V1_PAYLOAD_SHAPE,
        raw_record_count=raw_record_count,
        normalized_record_count=0,
        rejected_record_count=0,
        warnings=(error,),
        schema_fingerprint=greenhouse_schema_fingerprint(),
    )


def source_message(config, configured_url, detail):
    configured = f" Configured source: {configured_url}." if configured_url else ""
    return (
        f"{config.source_name} Greenhouse board {config.board_token!r}: {detail}."
        f"{configured}"
    )


def greenhouse_schema_fingerprint():
    contract = {
        "platform_contract_version": "greenhouse-job-board-v1",
        "endpoint_roles": {
            "authoritative_inventory": "board jobs full array",
            "required_enrichment": "department tree",
        },
        "roots": {
            "jobs": JOBS_ROOT_SCHEMA,
            "department_tree": DEPARTMENT_NODE_SCHEMA,
        },
        "records": {
            "job": JOB_RECORD_SCHEMA,
            "job_optional_public": JOB_OPTIONAL_PUBLIC_SCHEMA,
            "job_department": JOB_DEPARTMENT_SCHEMA,
            "job_office": JOB_OFFICE_SCHEMA,
            "department_job": DEPARTMENT_JOB_SCHEMA,
        },
    }
    serialized = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"greenhouse-job-board-v1:sha256:{digest}"


def additive_field_warnings(payload):
    warnings = []
    extra_root = sorted(set(payload) - {"jobs", "meta"})
    extra_meta = sorted(set(payload["meta"]) - {"total"})
    if extra_root:
        warnings.append(f"Greenhouse jobs response includes additive root fields: {extra_root}.")
    if extra_meta:
        warnings.append(f"Greenhouse jobs meta includes additive fields: {extra_meta}.")
    return warnings


def is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def strict_text(value):
    if not isinstance(value, str):
        return None
    return clean_value(value)


def type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    return type(value).__name__


# Legacy compatibility path. Existing Greenhouse consumers remain deliberately
# non-authoritative until each board has its own fixtures and completeness proof.
def fetch_greenhouse_jobs(api_url):
    data = request_json(api_url)

    if is_department_tree(data):
        return parse_greenhouse_department_tree(data)

    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        raise ValueError("Greenhouse response did not include a jobs list.")
    return [parse_greenhouse_job(job) for job in jobs]


def is_department_tree(data):
    return isinstance(data, dict) and (
        isinstance(data.get("children"), list)
        or isinstance(data.get("departments"), list)
    )


def parse_greenhouse_department_tree(root):
    parsed = []

    def walk(node, path):
        node_name = clean_value(node.get("name")) if isinstance(node, dict) else None
        current_path = [*path, node_name] if node_name else path

        for job in node.get("jobs") or []:
            department_path = " > ".join(current_path) if current_path else None
            parsed.append(parse_greenhouse_job(job, department_path=department_path))

        children = []
        children.extend(node.get("children") or [])
        children.extend(node.get("departments") or [])
        for child in children:
            if isinstance(child, dict):
                walk(child, current_path)

    walk(root, [])
    return dedupe_jobs(parsed)


def parse_greenhouse_job(job, department_path=None):
    department = department_path or extract_departments(job)
    return JobCandidate(
        external_id=clean_value(job.get("id")),
        title=clean_value(job.get("title")),
        location=clean_value(extract_location(job)),
        url=clean_value(job.get("absolute_url")),
        department=clean_value(department),
        expertise=clean_value(extract_expertise(department)),
        commitment=None,
    )


def extract_location(job):
    location = job.get("location")
    if isinstance(location, dict):
        return location.get("name")
    if isinstance(location, str):
        return location
    return None


def extract_departments(job):
    departments = job.get("departments")
    if not isinstance(departments, list):
        return None
    names = [
        department.get("name")
        for department in departments
        if isinstance(department, dict) and department.get("name")
    ]
    return ", ".join(names) if names else None


def extract_expertise(department):
    if not department:
        return None
    paths = [path.strip() for path in str(department).split("|") if path.strip()]
    categories = []
    for path in paths:
        parts = [part.strip() for part in path.split(">") if part.strip()]
        category = parts[1] if len(parts) >= 2 else (parts[0] if parts else None)
        if category and category not in categories:
            categories.append(category)
    return ", ".join(categories) or None


def dedupe_jobs(jobs):
    unique = []
    seen = set()
    for job in jobs:
        key = job.external_id or job.url
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


def clean_value(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None

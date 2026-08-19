"""Candidate-facing public catalog backed by trusted canonical opportunities."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import math
import re
import unicodedata
from urllib.parse import parse_qs, unquote_to_bytes, urlencode, urlsplit

from wahojobs import public_job_page
from wahojobs.matching.locations import (
    COUNTRY_REGION,
    LOCATION_SCOPE_REMOTE_RESTRICTED,
    LOCATION_SCOPE_REMOTE_WORLDWIDE,
    REGION_AMERICAS,
    REGION_APAC,
    REGION_EMEA,
    REMOTE_STATUS_HYBRID,
    REMOTE_STATUS_ONSITE,
    REMOTE_STATUS_REMOTE,
    canonical_country_from_text,
)
from wahojobs.opportunity_enrichment import (
    blank_document,
    resolve_effective_enrichments,
)
from wahojobs.matching.opportunity_trust import (
    freshness_max_age_hours,
    parse_utc,
)


PUBLIC_JOBS_ROUTE = "/jobs"
CATALOG_FILTER_KEYS = frozenset(
    {"q", "location", "work", "field", "language", "arrangement"}
)
CATALOG_QUERY_KEYS = CATALOG_FILTER_KEYS | {"page"}
CATALOG_FACET_KEYS = ("location", "work", "field", "language", "arrangement")
PAGE_SIZE = 30
CATALOG_CACHE_MAX_AGE_SECONDS = 300
MAX_FILTER_VALUE_CHARACTERS = 100
MAX_FILTER_VALUE_BYTES = 400
MAX_PAGE = 99_999
PAGE_VALUE = re.compile(r"^[1-9][0-9]{0,4}$")
INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
PROFESSIONAL_FIELD_LABELS = {
    "biology": "Biology",
    "chemistry": "Chemistry",
    "finance": "Finance",
    "legal": "Legal",
    "material_science": "Materials science",
    "mathematics": "Mathematics",
    "medicine": "Medicine",
    "physics": "Physics",
    "technical": "Software engineering",
}
WORK_ACTIVITY_LABELS = {
    "ads_evaluation": "Ads evaluation",
    "ai_training_evaluation": "AI training & evaluation",
    "audio_speech": "Audio & speech",
    "content_moderation": "Content moderation",
    "data_annotation": "Data annotation",
    "data_collection": "Data collection",
    "localization": "Localization",
    "operations": "Operations",
    "research_analysis": "Research & analysis",
    "search_evaluation": "Search evaluation",
    "software_development": "Software development",
    "software_testing": "Software testing",
    "transcription": "Transcription",
    "translation": "Translation",
    "writing_editing": "Writing & editing",
}
LOCATION_REGIONS = frozenset({REGION_AMERICAS, REGION_EMEA, REGION_APAC})


class CatalogPageOutOfRange(ValueError):
    pass


def load_public_jobs(connection, *, now=None):
    """Load one trusted active source variant per canonical opportunity."""

    rows = connection.execute(
        """
        SELECT
          j.id AS job_id,
          j.title AS source_title,
          j.location AS source_location,
          j.department AS source_department,
          j.expertise AS source_expertise,
          j.commitment AS source_commitment,
          j.url AS listing_url,
          j.external_id,
          j.opportunity_kind,
          j.availability_basis,
          j.include_in_live_market_estimate,
          j.first_seen_at AS job_first_seen_at,
          j.last_seen_at AS job_last_seen_at,
          j.is_active AS job_is_active,
          co.id AS canonical_opportunity_id,
          co.canonical_key,
          co.canonical_title,
          co.source_category,
          co.language AS canonical_language,
          co.language_locale AS canonical_language_locale,
          co.first_seen_at AS canonical_first_seen_at,
          co.last_seen_at AS canonical_last_seen_at,
          co.is_active AS canonical_is_active,
          c.name AS company_name,
          c.slug AS company_slug,
          c.careers_url,
          c.source_tier,
          c.inventory_model,
          c.market_count_policy,
          sc.provider AS rich_provider,
          sc.source_type AS rich_source_type,
          sc.source_url AS rich_source_url,
          sc.metadata_json AS rich_metadata_json,
          sc.source_updated_at,
          sc.first_captured_at,
          sc.last_captured_at,
          CASE WHEN sc.job_id IS NULL THEN 0 ELSE 1 END AS has_rich_content,
          oe.status AS enrichment_status,
          source_run.id AS source_run_id,
          source_run.started_at AS source_run_started_at,
          COALESCE(source_run.finished_at, source_run.started_at)
            AS latest_successful_source_run_at,
          CASE WHEN source_run.id IS NULL THEN 0 ELSE 1 END
            AS source_run_qualifies
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        JOIN canonical_opportunities co ON co.id = j.canonical_opportunity_id
        LEFT JOIN opportunity_enrichments oe
          ON oe.canonical_opportunity_id = co.id
        LEFT JOIN job_source_contents sc ON sc.job_id = j.id
        LEFT JOIN crawl_runs source_run
          ON source_run.id = (
            SELECT cr.id
            FROM crawl_runs cr
            WHERE cr.company_id = c.id
              AND cr.status = 'success'
              AND cr.used_sample_data = 0
              AND cr.error_message IS NULL
            ORDER BY COALESCE(cr.finished_at, cr.started_at) DESC, cr.id DESC
            LIMIT 1
          )
        WHERE j.is_active = 1
          AND co.is_active = 1
          AND j.title NOT LIKE '[SIMULATION]%'
        ORDER BY co.id, j.id
        """
    ).fetchall()

    grouped = {}
    for raw in rows:
        row = dict(raw)
        if not public_job_page.public_opportunity_is_eligible(row, now=now):
            continue
        row["_catalog_official_url"] = public_job_page.first_human_facing_url(
            row.get("rich_source_url"),
            row.get("listing_url"),
        )
        grouped.setdefault(int(row["canonical_opportunity_id"]), []).append(row)

    representatives = {
        canonical_id: max(variants, key=representative_variant_rank)
        for canonical_id, variants in grouped.items()
    }
    effective_by_canonical = resolve_effective_enrichments(
        connection,
        representatives,
    )

    jobs = []
    for canonical_id, row in representatives.items():
        effective = effective_by_canonical.get(canonical_id)
        document = effective["document"] if effective is not None else blank_document()
        job = dict(row)
        job.update(
            path=public_job_page.public_job_path(canonical_id),
            official_url=row["_catalog_official_url"],
            careers_url=public_job_page.human_facing_company_url(row["careers_url"]),
            enrichment=document,
            enrichment_field_sources=(effective or {}).get("field_sources", {}),
            overridden_fields=(effective or {}).get("overridden_fields", []),
            stale_override_fields=(effective or {}).get("stale_override_fields", []),
        )
        prepare_catalog_presentation(job)
        jobs.append(job)
    return jobs


def representative_variant_rank(row):
    return public_job_page.representative_variant_rank(row)


def prepare_catalog_presentation(job):
    attributes = job["enrichment"]["attributes"]
    role = attributes["role"]
    arrangement = attributes["work_arrangement"]
    requirements = attributes["requirements"]
    content = attributes["content"]
    source_location = candidate_text(job.get("source_location"))
    eligibility = public_job_page.candidate_eligibility(
        arrangement,
        source_location,
        variant_label=public_job_page.source_variant_label(job),
    )
    mode = eligibility["mode"]
    scope = eligibility["scope"]
    eligible_countries = set(eligibility["countries"])
    eligible_regions = set(eligibility["regions"])

    location_labels = set(eligible_countries) | set(eligible_regions)
    job["_catalog_location_model"] = {
        "scope": scope,
        "mode": mode,
        "countries": frozenset(eligible_countries),
        "regions": frozenset(eligible_regions),
    }

    work_labels = public_job_page.unique_text(
        work_activity_label(value)
        for value in role.get("work_activities") or []
    )
    field_labels = public_job_page.unique_text(
        professional_field_label(value)
        for value in role.get("professional_domains") or []
    )

    language_values = [
        candidate_text(item.get("language"))
        for item in public_job_page.candidate_language_requirements(
            job,
            requirements,
        )
    ]
    if not any(language_values):
        language_values.append(candidate_text(job.get("canonical_language")))
    language_labels = public_job_page.unique_text(language_values)

    engagement = public_job_page.clean(arrangement.get("engagement_type"))
    if not engagement or engagement == "unknown":
        engagement = engagement_type_from_source(job.get("source_commitment"))
    arrangement_labels = public_job_page.unique_text(
        [engagement_label(engagement)]
    )

    remote = eligibility["summary"]
    job["catalog_location"] = eligibility["summary"]
    job["catalog_summary"] = concise_summary(content.get("quick_take"))
    job["_catalog_engagement"] = engagement
    candidate_labels = {
        "location": location_labels,
        "work": work_labels,
        "field": field_labels,
        "language": language_labels,
        "arrangement": arrangement_labels,
    }
    job["_catalog_filter_values"] = {}
    job["_catalog_filter_labels"] = {}
    for key, labels in candidate_labels.items():
        candidate_values = set()
        labels_by_key = {}
        for raw_label in labels:
            label = candidate_text(raw_label)
            if not label:
                continue
            candidate_key = facet_value_key(label)
            if not candidate_key:
                continue
            candidate_values.add(candidate_key)
            labels_by_key.setdefault(candidate_key, label)
        job["_catalog_filter_values"][key] = candidate_values
        job["_catalog_filter_labels"][key] = labels_by_key
    search_values = [
        job.get("source_title"),
        job.get("canonical_title"),
        job.get("company_name"),
        source_location,
        remote,
        job.get("source_department"),
        job.get("source_expertise"),
        job.get("source_category"),
        job["catalog_summary"],
        *eligible_countries,
        *eligible_regions,
        *work_labels,
        *field_labels,
        *language_labels,
        *arrangement_labels,
    ]
    job["_catalog_search"] = normalize_search(" ".join(filter(None, search_values)))


def build_catalog(jobs, params=None):
    requested = normalize_catalog_params(params or {})
    facets = catalog_facets(jobs)
    filters, resolved_filters = resolve_candidate_filters(requested, facets)
    visible = [
        job for job in jobs if catalog_job_matches(job, filters, resolved_filters)
    ]
    visible = order_catalog_results(visible, filters.get("q"))
    result_count = len(visible)
    total_pages = max(1, math.ceil(result_count / PAGE_SIZE))
    requested_page = requested.get("page", 1)
    if requested_page > total_pages:
        raise CatalogPageOutOfRange("catalog_page_out_of_range")
    page = requested_page
    first_index = (page - 1) * PAGE_SIZE
    page_jobs = visible[first_index : first_index + PAGE_SIZE]
    return {
        "jobs": page_jobs,
        "inventory_count": len(jobs),
        "result_count": result_count,
        "page_result_count": len(page_jobs),
        "first_result_number": first_index + 1 if page_jobs else 0,
        "last_result_number": first_index + len(page_jobs),
        "page": page,
        "page_size": PAGE_SIZE,
        "total_pages": total_pages,
        "facets": facets,
        "filters": filters,
        "normalized_target": catalog_target(filters, page=page),
    }


def normalize_catalog_params(params):
    result = {}
    query = public_job_page.clean(params.get("q"))
    if query:
        result["q"] = query[:120]
    for key in CATALOG_FACET_KEYS:
        value = public_job_page.clean(params.get(key))
        if value and valid_filter_value(value):
            result[key] = value
    page = params.get("page")
    if type(page) is int and 1 <= page <= MAX_PAGE:
        result["page"] = page
    elif type(page) is str and PAGE_VALUE.fullmatch(page):
        result["page"] = int(page)
    return result


def resolve_candidate_filters(requested, facets):
    filters = {"q": requested["q"]} if requested.get("q") else {}
    resolved = {}
    for key in CATALOG_FACET_KEYS:
        value = requested.get(key)
        if not value:
            continue
        candidate_key = facet_value_key(value)
        option = next(
            (
                item
                for item in facets[key]
                if item["key"] == candidate_key
            ),
            None,
        )
        filters[key] = option["label"] if option is not None else value
        resolved[key] = option["key"] if option is not None else candidate_key
    return filters, resolved


def catalog_job_matches(job, filters, resolved_filters):
    query = normalize_search(filters.get("q"))
    if query and not all(term in job["_catalog_search"] for term in query.split()):
        return False
    for key in CATALOG_FACET_KEYS:
        selected = resolved_filters.get(key)
        if key == "location" and selected:
            if not location_filter_matches(job, filters[key]):
                return False
        elif selected and selected not in job["_catalog_filter_values"][key]:
            return False
    return True


def catalog_facets(jobs):
    counters = {key: Counter() for key in CATALOG_FACET_KEYS}
    labels = {key: {} for key in counters}
    for job in jobs:
        for key in counters:
            for token in job["_catalog_filter_values"][key]:
                counters[key][token] += 1
            labels[key].update(job["_catalog_filter_labels"][key])
    location_identities = {
        candidate_key: location_filter_identity(label)
        for candidate_key, label in labels["location"].items()
    }
    for candidate_key, identity in location_identities.items():
        counters["location"][candidate_key] = sum(
            location_model_matches(job["_catalog_location_model"], identity)
            for job in jobs
        )
    return {
        key: [
            {
                "key": candidate_key,
                "value": labels[key][candidate_key],
                "label": labels[key][candidate_key],
                "count": count,
            }
            for candidate_key, count in sorted(
                counters[key].items(),
                key=lambda item: facet_sort_key(
                    key,
                    item[0],
                    labels[key].get(item[0], ""),
                ),
            )
            if count
        ]
        for key in counters
    }


def parse_catalog_query(query):
    if not valid_query_encoding(query):
        return None
    try:
        raw = (
            parse_qs(
                query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=len(CATALOG_QUERY_KEYS),
            )
            if query
            else {}
        )
    except (UnicodeError, ValueError):
        return None
    if (
        set(raw) - CATALOG_QUERY_KEYS
        or any(type(values) is not list or len(values) != 1 for values in raw.values())
    ):
        return None
    params = {
        key: values[0].strip()
        for key, values in raw.items()
        if values[0].strip()
    }
    query_value = params.get("q")
    if query_value is not None and (
        len(query_value) > 120 or len(query_value.encode("utf-8")) > 480
    ):
        return None
    for key in CATALOG_FACET_KEYS:
        value = params.get(key)
        if value is not None and not valid_filter_value(value):
            return None
    page = params.get("page")
    if page is not None and (
        PAGE_VALUE.fullmatch(page) is None or int(page) > MAX_PAGE
    ):
        return None
    params["_query_present"] = bool(query)
    params["_raw_query"] = query
    return params


def valid_query_encoding(query):
    if type(query) is not str or INVALID_PERCENT_ESCAPE.search(query):
        return False
    try:
        unquote_to_bytes(query).decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return False
    return True


def validate_catalog_return_target(value):
    if type(value) is not str or not value or len(value.encode("utf-8")) > 1_500:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.path != PUBLIC_JOBS_ROUTE
        or parsed.fragment
    ):
        return None
    params = parse_catalog_query(parsed.query)
    if params is None:
        return None
    params.pop("_query_present", None)
    params.pop("_raw_query", None)
    normalized = normalize_catalog_params(params)
    return catalog_target(
        {key: value for key, value in normalized.items() if key != "page"},
        page=normalized.get("page", 1),
    )


def valid_filter_value(value):
    return (
        type(value) is str
        and value == value.strip()
        and 1 <= len(value) <= MAX_FILTER_VALUE_CHARACTERS
        and len(value.encode("utf-8")) <= MAX_FILTER_VALUE_BYTES
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    )


def render_public_jobs_page(
    catalog,
    *,
    public_origin,
    navigation="",
    query_present=False,
):
    filters = catalog["filters"]
    filters_present = bool(filters)
    canonical_url = (
        public_origin.rstrip("/") + catalog_target({}, page=catalog["page"])
        if not filters_present
        else None
    )
    robots = (
        "<meta name='robots' content='noindex,follow'>"
        if filters_present
        else ""
    )
    canonical = (
        f"<link rel='canonical' href='{public_job_page.e(canonical_url)}'>"
        if canonical_url
        else ""
    )
    q = filters.get("q", "")
    filter_controls = "".join(
        (
            render_filter_search(
                key,
                label,
                catalog["facets"][key],
                filters.get(key, ""),
            )
            for key, label in (
                ("location", "Where can you work from?"),
                ("work", "Type of work"),
                ("field", "Professional field"),
                ("language", "Language"),
            )
        )
    )
    clear = (
        f"<a class='clear-filters' href='{PUBLIC_JOBS_ROUTE}'>Clear filters</a>"
        if filters_present
        else ""
    )
    cards = "".join(
        render_job_card(job, return_to=None)
        for job in catalog["jobs"]
    )
    if cards:
        results = f"<section class='jobs-list' aria-label='Current jobs'>{cards}</section>"
    else:
        results = (
            "<section class='empty-results'><h2>No jobs match these filters</h2>"
            "<p>Try a broader keyword or clear one of the filters.</p></section>"
        )
    if catalog["result_count"]:
        count_label = (
            f"Showing {catalog['first_result_number']}–{catalog['last_result_number']} "
            f"of {catalog['result_count']} current opportunities"
        )
    else:
        count_label = "No current opportunities match"
    pagination = render_pagination(catalog)
    page_suffix = f" — Page {catalog['page']}" if catalog["page"] > 1 else ""
    description_suffix = f" Page {catalog['page']}." if catalog["page"] > 1 else ""

    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Browse jobs{public_job_page.e(page_suffix)} | Wahojobs</title>
  <meta name='description' content='Browse and search current Wahojobs opportunities.{public_job_page.e(description_suffix)}'>
  {robots}
  {canonical}
  <style>{public_job_page.PUBLIC_JOB_CSS}{PUBLIC_JOBS_CSS}</style>
</head>
<body>
  <header class='site-header'>
    <a class='brand' href='/jobs'>Wahojobs</a>
    {navigation}
  </header>
  <main class='catalog-main'>
    <header class='catalog-hero'>
      <p class='eyebrow'>Jobs</p>
      <h1>Browse current opportunities</h1>
      <p>Search Wahojobs opportunities and open any job for the available details and official application link.</p>
    </header>
    <form class='catalog-filters' method='get' action='/jobs' role='search'>
      <label class='keyword-field' for='jobs-q'>
        <span>Keyword</span>
        <input id='jobs-q' name='q' type='search' maxlength='120' value='{public_job_page.e(q)}' placeholder='Title, company, skill, or location'>
      </label>
      {filter_controls}
      <div class='filter-actions'>
        <button type='submit'>Search jobs</button>
        {clear}
      </div>
    </form>
    <div class='catalog-summary' aria-live='polite'><strong>{public_job_page.e(count_label)}</strong></div>
    {results}
    {pagination}
  </main>
</body>
</html>"""


def render_filter_search(name, label, options, selected):
    rendered = []
    for option in options:
        job_label = "job" if option["count"] == 1 else "jobs"
        rendered.append(
            f"<option value='{public_job_page.e(option['value'])}'>"
            f"{option['count']} {job_label}</option>"
        )
    list_id = "jobs-" + name + "-options"
    unavailable = not options and not selected
    disabled = " disabled aria-disabled='true'" if unavailable else ""
    placeholder = (
        "Not available yet"
        if unavailable
        else "Country or region"
        if name == "location"
        else "Type to search"
    )
    return (
        f"<label for='jobs-{public_job_page.e(name)}'><span>{public_job_page.e(label)}</span>"
        f"<input id='jobs-{public_job_page.e(name)}' name='{public_job_page.e(name)}' "
        f"list='{public_job_page.e(list_id)}' maxlength='{MAX_FILTER_VALUE_CHARACTERS}' "
        f"value='{public_job_page.e(selected)}' placeholder='{public_job_page.e(placeholder)}' "
        f"autocomplete='off'{disabled}>"
        f"<datalist id='{public_job_page.e(list_id)}'>"
        + "".join(rendered)
        + "</datalist></label>"
    )


def render_job_card(job, *, return_to):
    title = candidate_text(job.get("source_title")) or candidate_text(
        job.get("canonical_title")
    )
    company = candidate_text(job.get("company_name"))
    location = job.get("catalog_location")
    location_html = f"<p class='job-location'>{public_job_page.e(location)}</p>" if location else ""
    summary = job.get("catalog_summary")
    summary_html = f"<p class='job-summary'>{public_job_page.e(summary)}</p>" if summary else ""
    attributes = catalog_card_attributes(job)[:3]
    chips = (
        "<ul class='card-attributes'>"
        + "".join(f"<li>{public_job_page.e(value)}</li>" for value in attributes)
        + "</ul>"
        if attributes
        else ""
    )
    detail_target = job["path"]
    return f"""
    <article class='job-card'>
      <div class='job-card-copy'>
        {f"<p class='job-company'>{public_job_page.e(company)}</p>" if company else ""}
        <h2><a href='{public_job_page.e(detail_target)}'>{public_job_page.e(title)}</a></h2>
        {location_html}
        {chips}
        {summary_html}
      </div>
      <a class='view-job' href='{public_job_page.e(detail_target)}' aria-label='View {public_job_page.e(title)} at {public_job_page.e(company)}'>View job</a>
    </article>
    """


def render_pagination(catalog):
    if catalog["total_pages"] <= 1:
        return ""
    page = catalog["page"]
    previous_link = (
        pagination_link(catalog, page - 1, "Previous") if page > 1 else ""
    )
    next_link = (
        pagination_link(catalog, page + 1, "Next")
        if page < catalog["total_pages"]
        else ""
    )
    return f"""
    <nav class='pagination' aria-label='Jobs pages'>
      <span class='pagination-side previous'>{previous_link}</span>
      <span>Page {page} of {catalog['total_pages']}</span>
      <span class='pagination-side next'>{next_link}</span>
    </nav>
    """


def pagination_link(catalog, page, label):
    target = catalog_target(catalog["filters"], page=page)
    return f"<a href='{public_job_page.e(target)}'>{public_job_page.e(label)}</a>"


def catalog_card_attributes(job):
    attributes = job["enrichment"]["attributes"]
    role = attributes["role"]
    arrangement = attributes["work_arrangement"]
    requirements = attributes["requirements"]
    values = []
    activities = public_job_page.unique_text(
        work_activity_label(value)
        for value in role.get("work_activities") or []
    )
    fields = public_job_page.unique_text(
        professional_field_label(value)
        for value in role.get("professional_domains") or []
    )
    values.append(next(iter(activities), None))
    values.append(next(iter(fields), None))
    languages = [
        candidate_text(item.get("language"))
        for item in public_job_page.candidate_language_requirements(
            job,
            requirements,
        )
    ]
    if not any(languages):
        languages.append(candidate_text(job.get("canonical_language")))
    values.append(next(iter(public_job_page.unique_text(languages)), None))
    values.append(engagement_label(job.get("_catalog_engagement")))
    return public_job_page.unique_text(values)


def concise_summary(value, limit=240):
    if not candidate_text(value):
        return None
    value = public_job_page.as_sentence(value)
    if not value or len(value) <= limit:
        return value
    shortened = value[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return shortened + "…" if shortened else value[: limit - 1] + "…"


def order_catalog_results(jobs, query):
    if query:
        return sorted(jobs, key=lambda job: catalog_result_sort_key(job, query))
    return diversify_companies_by_recency(jobs)


def diversify_companies_by_recency(jobs):
    """Interleave provider batches while preserving recency within each company."""

    queues = {}
    for job in jobs:
        company_key = (
            public_job_page.clean(job.get("company_slug"))
            or public_job_page.clean(job.get("company_name"))
            or f"job-{job['job_id']}"
        ).casefold()
        queues.setdefault(company_key, []).append(job)
    for queue in queues.values():
        queue.sort(key=lambda job: catalog_result_sort_key(job, None))

    positions = {company_key: 0 for company_key in queues}
    result = []
    last_company = None
    while len(result) < len(jobs):
        active = [
            company_key
            for company_key, queue in queues.items()
            if positions[company_key] < len(queue)
        ]
        active.sort(
            key=lambda company_key: (
                catalog_result_sort_key(
                    queues[company_key][positions[company_key]],
                    None,
                ),
                company_key,
            )
        )
        if len(active) > 1 and active[0] == last_company:
            active.append(active.pop(0))
        for company_key in active:
            result.append(queues[company_key][positions[company_key]])
            positions[company_key] += 1
            last_company = company_key
    return result


def catalog_result_sort_key(job, query):
    recency = trustworthy_recency_value(job)
    stable = (
        -int(job["job_id"]),
        (public_job_page.clean(job.get("source_title")) or "").casefold(),
        (public_job_page.clean(job.get("company_name")) or "").casefold(),
    )
    if query:
        return (-search_relevance(job, query), -recency, *stable)
    return (-recency, *stable)


def search_relevance(job, query):
    phrase = normalize_search(query)
    if not phrase:
        return 0
    terms = phrase.split()
    title = normalize_search(job.get("source_title") or job.get("canonical_title"))
    canonical_title = normalize_search(job.get("canonical_title"))
    company = normalize_search(job.get("company_name"))
    location = normalize_search(job.get("catalog_location"))
    category = normalize_search(job.get("source_category"))
    expertise = normalize_search(job.get("source_expertise"))
    summary = normalize_search(job.get("catalog_summary"))
    title_terms = set(title.split())
    company_terms = set(company.split())
    score = 0
    if phrase == title:
        score += 1_000
    elif title.startswith(phrase + " "):
        score += 850
    elif phrase in title:
        score += 700
    elif all(term in title_terms for term in terms):
        score += 520
    score += sum(90 for term in terms if term in title_terms)
    if phrase == company:
        score += 400
    elif company.startswith(phrase + " "):
        score += 320
    elif phrase in company:
        score += 260
    score += sum(45 for term in terms if term in company_terms)
    if phrase in canonical_title and canonical_title != title:
        score += 180
    score += sum(35 for term in terms if term in category)
    score += sum(25 for term in terms if term in expertise)
    score += sum(20 for term in terms if term in location)
    score += sum(10 for term in terms if term in summary)
    return score


def trustworthy_recency_value(job):
    for field in (
        "source_updated_at",
        "job_first_seen_at",
        "canonical_first_seen_at",
    ):
        value = parsed_timestamp(job.get(field))
        if value is not None:
            return value
    return 0.0


def parsed_timestamp(value):
    value = public_job_page.clean(value)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def catalog_cache_deadline(jobs, loaded_at):
    """Bound a snapshot by both response caching and the next trust expiry."""

    deadline = loaded_at + timedelta(seconds=CATALOG_CACHE_MAX_AGE_SECONDS)
    for job in jobs:
        max_age_hours = freshness_max_age_hours(
            public_job_page.clean(job.get("inventory_model")),
            public_job_page.clean(job.get("market_count_policy")),
        )
        source_run_at = parse_utc(
            public_job_page.clean(job.get("latest_successful_source_run_at"))
        )
        if max_age_hours is None or source_run_at is None:
            continue
        deadline = min(
            deadline,
            source_run_at + timedelta(hours=max_age_hours),
        )
    return deadline


def catalog_target(filters, *, page=1):
    params = []
    for key in ("q", *CATALOG_FACET_KEYS):
        value = public_job_page.clean(filters.get(key))
        if value:
            params.append((key, value))
    if page > 1:
        params.append(("page", str(page)))
    return PUBLIC_JOBS_ROUTE + ("?" + urlencode(params) if params else "")


def facet_sort_key(group, candidate_key, label):
    return (0, label.casefold(), candidate_key)


def normalize_search(value):
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii").casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def facet_value_key(value):
    return normalize_search(value)


def candidate_text(value):
    value = public_job_page.clean(value)
    return None if not value or normalize_search(value) == "unknown" else value


def professional_field_label(value):
    return PROFESSIONAL_FIELD_LABELS.get(public_job_page.clean(value))


def work_activity_label(value):
    return WORK_ACTIVITY_LABELS.get(public_job_page.clean(value))


def location_filter_identity(value):
    key = facet_value_key(value)
    special = {
        facet_value_key("Remote"): ("remote", None),
        facet_value_key("Remote worldwide"): ("remote_worldwide", None),
        facet_value_key("Remote with location limits"): (
            "remote_restricted",
            None,
        ),
        facet_value_key("Hybrid"): ("hybrid", None),
        facet_value_key("On-site"): ("onsite", None),
    }
    if key in special:
        return special[key]
    region = next(
        (region for region in LOCATION_REGIONS if facet_value_key(region) == key),
        None,
    )
    if region:
        return "region", region
    country = canonical_country_from_text(value)
    return ("country", country) if country else (None, None)


def location_filter_matches(job, selected):
    return location_model_matches(
        job["_catalog_location_model"],
        location_filter_identity(selected),
    )


def location_model_matches(model, identity):
    kind, value = identity
    if kind is None:
        return False
    scope = model["scope"]
    mode = model["mode"]
    if kind == "remote":
        return mode == REMOTE_STATUS_REMOTE or scope in {
            LOCATION_SCOPE_REMOTE_WORLDWIDE,
            LOCATION_SCOPE_REMOTE_RESTRICTED,
        }
    if kind == "remote_worldwide":
        return scope == LOCATION_SCOPE_REMOTE_WORLDWIDE
    if kind == "remote_restricted":
        return scope == LOCATION_SCOPE_REMOTE_RESTRICTED
    if kind == "hybrid":
        return mode == REMOTE_STATUS_HYBRID
    if kind == "onsite":
        return mode == REMOTE_STATUS_ONSITE
    if scope == LOCATION_SCOPE_REMOTE_WORLDWIDE:
        return True
    if kind == "country":
        return value in model["countries"] or COUNTRY_REGION.get(value) in model[
            "regions"
        ]
    return kind == "region" and value in model["regions"]


def engagement_type_from_source(value):
    text = " " + normalize_search(value) + " "
    rules = (
        ("internship", (" internship ", " intern ")),
        ("volunteer", (" volunteer ",)),
        ("freelance", (" freelance ", " independent contractor ")),
        ("temporary", (" temporary ",)),
        ("contract", (" contract ", " project based ")),
        ("part_time", (" part time ",)),
        ("full_time", (" full time ",)),
    )
    for engagement, terms in rules:
        if any(term in text for term in terms):
            return engagement
    return None


def engagement_label(value):
    return {
        "full_time": "Full-time",
        "part_time": "Part-time",
    }.get(value, public_job_page.enum_label(value))


PUBLIC_JOBS_CSS = """
.catalog-main { padding-top: 8px; }
.catalog-hero { border: 1px solid #d9e2dd; border-radius: 18px 18px 0 0; background: #fff; padding: clamp(28px, 5vw, 52px); }
.catalog-hero h1 { max-width: 820px; }
.catalog-hero > p:last-child { max-width: 760px; margin: 18px 0 0; color: #4d6359; font-size: 1.06rem; }
.catalog-filters { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 14px; border: 1px solid #d9e2dd; border-top: 0; border-radius: 0 0 18px 18px; background: #f9fbfa; padding: 22px; }
.keyword-field { grid-column: span 2; }
.catalog-filters label { display: grid; align-content: start; gap: 6px; color: #40564c; font-size: .86rem; font-weight: 800; }
.catalog-filters input, .catalog-filters select { width: 100%; min-height: 44px; border: 1px solid #aebeb5; border-radius: 8px; background: #fff; color: #18231e; padding: 9px 10px; font: inherit; }
.catalog-filters input:disabled { background: #eef2f0; color: #6c7a73; cursor: not-allowed; }
.filter-actions { grid-column: 1 / -1; display: flex; align-items: center; gap: 16px; }
.filter-actions button { border: 0; border-radius: 8px; background: #176b52; color: #fff; padding: 11px 18px; font: inherit; font-weight: 800; cursor: pointer; }
.clear-filters { font-size: .92rem; }
.catalog-summary { padding: 28px 2px 14px; color: #40564c; }
.jobs-list { display: grid; gap: 12px; }
.job-card { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 24px; align-items: center; border: 1px solid #d9e2dd; border-radius: 13px; background: #fff; padding: 22px 24px; }
.job-company { margin: 0 0 5px; color: #527064; font-size: .84rem; font-weight: 800; letter-spacing: .035em; text-transform: uppercase; }
.job-card h2 { margin: 0 0 7px; font-size: clamp(1.15rem, 2vw, 1.42rem); }
.job-card h2 a { color: #18231e; text-decoration-thickness: 1px; text-underline-offset: 3px; }
.job-location { margin: 0; color: #4d6359; font-weight: 650; }
.card-attributes { display: flex; flex-wrap: wrap; gap: 7px; margin: 13px 0 0; padding: 0; list-style: none; }
.card-attributes li { border-radius: 999px; background: #edf5f1; color: #315447; padding: 4px 9px; font-size: .82rem; font-weight: 750; }
.job-summary { max-width: 820px; margin: 14px 0 0; color: #354b41; }
.view-job { white-space: nowrap; border-radius: 8px; background: #e2f0e9; padding: 9px 14px; text-decoration: none; }
.empty-results { border: 1px solid #d9e2dd; border-radius: 13px; background: #fff; padding: 34px; text-align: center; }
.empty-results p { margin-bottom: 0; color: #52665c; }
.pagination { display: grid; grid-template-columns: 1fr auto 1fr; gap: 18px; align-items: center; margin: 24px 0 0; border: 1px solid #d9e2dd; border-radius: 11px; background: #fff; padding: 14px 18px; color: #52665c; font-weight: 750; }
.pagination-side.next { text-align: right; }
@media (max-width: 1000px) { .catalog-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); } .keyword-field { grid-column: 1 / -1; } }
@media (max-width: 680px) { .catalog-filters { grid-template-columns: 1fr; } .keyword-field, .filter-actions { grid-column: auto; } .job-card { grid-template-columns: 1fr; gap: 16px; } .view-job { justify-self: start; } }
"""


__all__ = [
    "CatalogPageOutOfRange",
    "CATALOG_FILTER_KEYS",
    "CATALOG_QUERY_KEYS",
    "PAGE_SIZE",
    "PUBLIC_JOBS_ROUTE",
    "build_catalog",
    "catalog_cache_deadline",
    "catalog_target",
    "load_public_jobs",
    "parse_catalog_query",
    "render_job_card",
    "render_public_jobs_page",
    "valid_query_encoding",
    "validate_catalog_return_target",
]

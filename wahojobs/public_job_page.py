"""Canonical opportunity-backed public job-page presentation.

This module owns no routes, authentication, or mutations.  It reads one
persisted source listing together with its canonical opportunity and effective
V2 enrichment, then renders only facts that those records establish.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import html
import json
import math
import re
from urllib.parse import urlsplit

from wahojobs.classification import (
    AVAILABILITY_BASIS_API_FEED,
    AVAILABILITY_BASIS_EVERGREEN_PAGE,
    AVAILABILITY_BASIS_LOGIN_GATED_AFTER_APPLY,
    AVAILABILITY_BASIS_PUBLIC_CMS,
    AVAILABILITY_BASIS_PUBLIC_FEED,
    AVAILABILITY_BASIS_PUBLIC_PAGE,
    INVENTORY_MODEL_EVERGREEN_APPLICATION,
    INVENTORY_MODEL_LIVE_FEED,
    INVENTORY_MODEL_MIXED,
    INVENTORY_MODEL_PUBLIC_INVENTORY,
    MARKET_COUNT_POLICY_COUNT_LIVE,
    MARKET_COUNT_POLICY_EXCLUDE_LIVE_ESTIMATE,
    MARKET_COUNT_POLICY_REPORT_SEPARATELY,
    OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
    OPPORTUNITY_KIND_LIVE_POSTING,
    OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY,
)
from wahojobs.matching.locations import (
    LOCATION_SCOPE_REMOTE_RESTRICTED,
    LOCATION_SCOPE_REMOTE_WORLDWIDE,
    REGION_AMERICAS,
    REGION_APAC,
    REGION_EMEA,
    REMOTE_STATUS_REMOTE,
    canonical_country_from_text,
    classify_job_location,
    countries_in_location,
    regions_in_location,
)
from wahojobs.matching.languages import find_language_mentions
from wahojobs.matching.opportunity_trust import TRUSTED, assess_opportunity_trust
from wahojobs.opportunity_enrichment import (
    MIN_LLM_BODY_CHARACTERS,
    blank_document,
    resolve_effective_enrichment,
    source_body_paragraphs,
    source_body_text,
)


PUBLIC_JOB_PATH = re.compile(
    r"^/job/opportunity-(?P<canonical_opportunity_id>[1-9][0-9]*)$"
)
PUBLIC_JOB_SLUG = re.compile(
    r"^opportunity-(?P<canonical_opportunity_id>[1-9][0-9]*)$"
)
PUBLIC_COMPANY_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PUBLIC_JOB_STATE_LIVE = "live"
PUBLIC_JOB_STATE_TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
PUBLIC_SOURCE_TIERS = frozenset({"core", "strategic"})
PUBLIC_INVENTORY_POLICY_BY_MODEL = {
    INVENTORY_MODEL_LIVE_FEED: frozenset({MARKET_COUNT_POLICY_COUNT_LIVE}),
    INVENTORY_MODEL_EVERGREEN_APPLICATION: frozenset(
        {
            MARKET_COUNT_POLICY_REPORT_SEPARATELY,
            MARKET_COUNT_POLICY_EXCLUDE_LIVE_ESTIMATE,
        }
    ),
    INVENTORY_MODEL_MIXED: frozenset(
        {
            MARKET_COUNT_POLICY_REPORT_SEPARATELY,
            MARKET_COUNT_POLICY_EXCLUDE_LIVE_ESTIMATE,
        }
    ),
    INVENTORY_MODEL_PUBLIC_INVENTORY: frozenset(
        {
            MARKET_COUNT_POLICY_REPORT_SEPARATELY,
            MARKET_COUNT_POLICY_EXCLUDE_LIVE_ESTIMATE,
        }
    ),
}
PUBLIC_OPPORTUNITY_KINDS_BY_MODEL = {
    INVENTORY_MODEL_LIVE_FEED: frozenset({OPPORTUNITY_KIND_LIVE_POSTING}),
    INVENTORY_MODEL_EVERGREEN_APPLICATION: frozenset(
        {OPPORTUNITY_KIND_EVERGREEN_APPLICATION}
    ),
    INVENTORY_MODEL_PUBLIC_INVENTORY: frozenset(
        {OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY}
    ),
    INVENTORY_MODEL_MIXED: frozenset(
        {
            OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
            OPPORTUNITY_KIND_LIVE_POSTING,
            OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY,
        }
    ),
}
PUBLIC_AVAILABILITY_BASES = frozenset(
    {
        AVAILABILITY_BASIS_API_FEED,
        AVAILABILITY_BASIS_EVERGREEN_PAGE,
        AVAILABILITY_BASIS_LOGIN_GATED_AFTER_APPLY,
        AVAILABILITY_BASIS_PUBLIC_CMS,
        AVAILABILITY_BASIS_PUBLIC_FEED,
        AVAILABILITY_BASIS_PUBLIC_PAGE,
    }
)
MAX_PUBLIC_JOB_PAGE_BYTES = 8_388_608
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807


def public_job_slug(canonical_opportunity_id):
    identifier = str(canonical_opportunity_id or "")
    candidate = f"opportunity-{identifier}"
    return (
        candidate
        if PUBLIC_JOB_SLUG.fullmatch(candidate) is not None
        and int(identifier) <= MAX_SQLITE_INTEGER
        else None
    )


def public_job_path(canonical_opportunity_id):
    slug = public_job_slug(canonical_opportunity_id)
    return f"/job/{slug}" if slug is not None else None


def public_company_path(company_slug):
    slug = str(company_slug or "")
    return f"/company/{slug}" if PUBLIC_COMPANY_SLUG.fullmatch(slug) else None


def public_job_path_for_match(match):
    if not isinstance(match, dict):
        return None
    return public_job_path(match.get("canonical_opportunity_id"))


def parse_public_job_path(path):
    if not isinstance(path, str):
        return None
    match = PUBLIC_JOB_PATH.fullmatch(path)
    if match is None:
        return None
    identifier = int(match.group("canonical_opportunity_id"))
    return identifier if identifier <= MAX_SQLITE_INTEGER else None


def load_public_job(connection, path, *, now=None):
    """Load one durable canonical opportunity and its best source variant."""

    canonical_opportunity_id = parse_public_job_path(path)
    if canonical_opportunity_id is None:
        return None
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
          j.removed_at,
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
          sc.external_id AS rich_external_id,
          sc.body AS rich_body,
          sc.body_format AS rich_body_format,
          sc.metadata_json AS rich_metadata_json,
          sc.material_content_sha256,
          sc.source_updated_at,
          sc.first_captured_at,
          sc.last_captured_at,
          CASE WHEN sc.job_id IS NULL THEN 0 ELSE 1 END AS has_rich_content,
          oe.status AS enrichment_status,
          oe.model_provider,
          oe.model_name,
          oe.generated_at AS enrichment_generated_at,
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
        WHERE co.id = ?
          AND j.title NOT LIKE '[SIMULATION]%'
        ORDER BY j.id
        """,
        (canonical_opportunity_id,),
    ).fetchall()
    historical = [
        dict(row)
        for row in rows
        if public_historical_inventory_is_eligible(dict(row))
    ]
    if not historical:
        return None

    current = [
        row for row in historical if public_opportunity_is_eligible(row, now=now)
    ]
    result = max(current or historical, key=representative_variant_rank)
    public_state = (
        PUBLIC_JOB_STATE_LIVE
        if current
        else PUBLIC_JOB_STATE_TEMPORARILY_UNAVAILABLE
    )

    effective = resolve_effective_enrichment(
        connection,
        int(result["canonical_opportunity_id"]),
    )
    document = effective["document"] if effective is not None else blank_document()
    official_url = first_human_facing_url(
        result["rich_source_url"],
        result["listing_url"],
    )
    careers_url = human_facing_company_url(result["careers_url"])
    result.update(
        path=public_job_path(result["canonical_opportunity_id"]),
        public_state=public_state,
        company_path=public_company_path(result["company_slug"]),
        official_url=official_url,
        careers_url=careers_url,
        enrichment=document,
        enrichment_field_sources=(effective or {}).get("field_sources", {}),
        overridden_fields=(effective or {}).get("overridden_fields", []),
        stale_override_fields=(effective or {}).get("stale_override_fields", []),
    )
    result["jobposting_evidence"] = (
        truthful_jobposting_evidence(result, now=now)
        if public_state == PUBLIC_JOB_STATE_LIVE
        else None
    )
    result["workflow_match"] = {
        "source": result["company_name"],
        "source_slug": result["company_slug"],
        "display_title": result["source_title"],
        "title": result["source_title"],
        "url": official_url or "",
        "job_id": result["job_id"],
        "canonical_opportunity_id": result["canonical_opportunity_id"],
        "job_is_active": bool(result["job_is_active"]),
        "canonical_is_active": bool(result["canonical_is_active"]),
        "_public_job_workflow": True,
    }
    return result


def public_opportunity_is_eligible(row, *, now=None):
    """Return whether one source variant is safe to present as a current job."""

    if (
        not bool(row.get("job_is_active"))
        or not bool(row.get("canonical_is_active"))
        or not public_historical_inventory_is_eligible(row)
    ):
        return False
    trust = assess_opportunity_trust(row, "unknown", now=now)
    return trust.status == TRUSTED


def public_historical_inventory_is_eligible(row):
    return bool(
        clean(row.get("source_tier")) in PUBLIC_SOURCE_TIERS
        and public_inventory_is_eligible(row)
        and public_job_classification_is_eligible(row)
        and public_company_path(row.get("company_slug")) is not None
        and public_job_path(row.get("canonical_opportunity_id")) is not None
    )


def public_inventory_is_eligible(row):
    policy = clean(row.get("market_count_policy"))
    model = clean(row.get("inventory_model"))
    return policy in PUBLIC_INVENTORY_POLICY_BY_MODEL.get(model, ())


def public_job_classification_is_eligible(row):
    """Fail closed for unknown, teaser, signal, or mismatched job classes."""

    model = clean(row.get("inventory_model"))
    policy = clean(row.get("market_count_policy"))
    kind = clean(row.get("opportunity_kind"))
    availability = clean(row.get("availability_basis"))
    if (
        kind not in PUBLIC_OPPORTUNITY_KINDS_BY_MODEL.get(model, ())
        or availability not in PUBLIC_AVAILABILITY_BASES
    ):
        return False
    return bool(row.get("include_in_live_market_estimate")) if (
        policy == MARKET_COUNT_POLICY_COUNT_LIVE
    ) else True


def representative_variant_rank(row):
    return (
        bool(row.get("has_rich_content")),
        bool(
            row.get("_catalog_official_url")
            or first_human_facing_url(
                row.get("rich_source_url"),
                row.get("listing_url"),
            )
        ),
        clean(row.get("job_last_seen_at")) or "",
        -int(row["job_id"]),
    )


def truthful_jobposting_evidence(job, *, now=None):
    """Return source-faithful evidence or None; never synthesize required facts."""

    if (
        job.get("public_state") != PUBLIC_JOB_STATE_LIVE
        or clean(job.get("rich_source_type")) != "greenhouse-job-board-v1"
        or not clean(job.get("source_title"))
        or not clean(job.get("company_name"))
        or not first_human_facing_url(job.get("official_url"))
    ):
        return None
    try:
        metadata = json.loads(job.get("rich_metadata_json") or "{}")
    except (TypeError, ValueError):
        return None
    if type(metadata) is not dict:
        return None
    original_posted = truthful_original_posting_date(
        metadata.get("first_published"),
        now=now,
    )
    if original_posted is None:
        return None

    paragraphs = tuple(
        source_body_paragraphs(
            job.get("rich_body"),
            job.get("rich_body_format"),
        )
    )
    description = "\n\n".join(paragraphs)
    if (
        len(source_body_text(
            job.get("rich_body"),
            job.get("rich_body_format"),
        )) < MIN_LLM_BODY_CHARACTERS
        or len(description) < MIN_LLM_BODY_CHARACTERS
    ):
        return None

    countries = supported_remote_source_countries(job.get("source_location"))
    if not countries:
        return None
    return {
        "original_posted": original_posted,
        "paragraphs": paragraphs,
        "description": description,
        "countries": countries,
    }


def supported_remote_source_countries(source_location):
    """Return only countries explicitly present in a remote source location."""

    source_location = candidate_text(source_location)
    scope, mode, _requirements, _restriction = classify_job_location(source_location)
    if mode != REMOTE_STATUS_REMOTE or scope != LOCATION_SCOPE_REMOTE_RESTRICTED:
        return ()
    return tuple(
        sorted(set(countries_in_location(source_location)), key=str.casefold)
    )


def truthful_original_posting_date(value, *, now=None):
    value = clean(value)
    if not value or len(value) > 64:
        return None
    current = now if now is not None else datetime.now(timezone.utc)
    if type(current) is not datetime or current.tzinfo is None:
        return None
    current = current.astimezone(timezone.utc)
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            parsed_date = date.fromisoformat(value)
            return value if parsed_date <= current.date() else None
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return value if parsed.astimezone(timezone.utc) <= current else None


def jobposting_fragments(job, canonical_url):
    evidence = job.get("jobposting_evidence")
    if not isinstance(evidence, dict):
        return "", "", None
    paragraphs = evidence.get("paragraphs")
    countries = evidence.get("countries")
    if not paragraphs or not countries:
        return "", "", None

    organization = {
        "@type": "Organization",
        "name": clean(job.get("company_name")),
    }
    company_url = human_facing_company_url(job.get("careers_url"))
    if company_url:
        organization["sameAs"] = company_url
    document = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "applicantLocationRequirements": [
            {"@type": "Country", "name": country}
            for country in countries
        ],
        "datePosted": evidence["original_posted"],
        "description": evidence["description"],
        "hiringOrganization": organization,
        "identifier": {
            "@type": "PropertyValue",
            "name": clean(job.get("company_name")),
            "value": f"wahojobs-opportunity-{int(job['canonical_opportunity_id'])}",
        },
        "jobLocationType": "TELECOMMUTE",
        "title": clean(job.get("source_title")),
        "url": canonical_url,
    }
    employment_type = {
        "full_time": "FULL_TIME",
        "part_time": "PART_TIME",
        "contract": "CONTRACTOR",
        "freelance": "CONTRACTOR",
        "temporary": "TEMPORARY",
        "internship": "INTERN",
        "volunteer": "VOLUNTEER",
    }.get(
        clean(
            job["enrichment"]["attributes"]["work_arrangement"].get(
                "engagement_type"
            )
        )
    )
    if employment_type:
        document["employmentType"] = employment_type
    salary = schema_base_salary(job["enrichment"]["attributes"]["compensation"])
    if salary is not None:
        document["baseSalary"] = salary
    deadline = source_supported_deadline(job["enrichment"])
    if deadline is not None:
        document["validThrough"] = deadline

    serialized = json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    script = f"<script type='application/ld+json'>{serialized}</script>"
    visible = (
        "<section class='content-section source-description' "
        "aria-labelledby='source-job-description'>"
        "<h2 id='source-job-description'>Source job description</h2>"
        + "".join(f"<p>{e(paragraph)}</p>" for paragraph in paragraphs)
        + "</section>"
    )
    return script, visible, evidence["original_posted"]


def schema_base_salary(compensation):
    if compensation.get("disclosed") is not True:
        return None
    currency = (clean(compensation.get("currency")) or "").upper()
    period = {
        "hour": "HOUR",
        "day": "DAY",
        "week": "WEEK",
        "month": "MONTH",
        "year": "YEAR",
    }.get(clean(compensation.get("period")))
    minimum = compensation.get("amount_min")
    maximum = compensation.get("amount_max")
    if (
        re.fullmatch(r"[A-Z]{3}", currency) is None
        or period is None
        or (minimum is None and maximum is None)
        or not schema_salary_number(minimum)
        or not schema_salary_number(maximum)
        or (
            minimum is not None
            and maximum is not None
            and minimum > maximum
        )
    ):
        return None
    value = {"@type": "QuantitativeValue", "unitText": period}
    if minimum is not None and maximum is not None and minimum == maximum:
        value["value"] = minimum
    else:
        if minimum is not None:
            value["minValue"] = minimum
        if maximum is not None:
            value["maxValue"] = maximum
    return {"@type": "MonetaryAmount", "currency": currency, "value": value}


def schema_salary_number(value):
    return bool(
        value is None
        or (
            type(value) in {int, float}
            and math.isfinite(value)
            and value >= 0
        )
    )


def source_supported_deadline(document):
    value = clean(document["attributes"]["application"].get("deadline"))
    if not value:
        return None
    supported = any(
        item.get("field_path") == "attributes.application.deadline"
        and item.get("basis") in {"source_explicit", "deterministic_parse"}
        for item in document.get("field_evidence") or []
        if isinstance(item, dict)
    )
    if not supported:
        return None
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            date.fromisoformat(value)
        else:
            candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
    except ValueError:
        return None
    return value


def render_public_job_page(
    job,
    *,
    public_origin,
    authenticated=False,
    navigation="",
    workflow_controls="",
    workflow_status="",
    workflow_script="",
    catalog_return_to=None,
):
    if job.get("public_state") != PUBLIC_JOB_STATE_LIVE:
        return render_unavailable_public_job_page(
            job,
            public_origin=public_origin,
            navigation=navigation,
            catalog_return_to=catalog_return_to,
        )
    document = job["enrichment"]
    attributes = document["attributes"]
    role = attributes["role"]
    arrangement = attributes["work_arrangement"]
    requirements = attributes["requirements"]
    compensation = attributes["compensation"]
    application = attributes["application"]
    content = attributes["content"]

    canonical_url = public_origin.rstrip("/") + job["path"]
    jobposting_script, source_description_section, original_posted = (
        jobposting_fragments(job, canonical_url)
    )
    page_title = clean(job["source_title"]) or clean(job["canonical_title"])
    company_name = clean(job["company_name"])
    catalog_return_to = safe_catalog_return_target(catalog_return_to)
    robots = (
        "<meta name='robots' content='noindex,follow'>"
        if catalog_return_to
        else ""
    )
    return_target = catalog_return_to or "/jobs"
    back_to_jobs = (
        f"<p class='back-to-jobs'><a href='{e(return_target)}'>← Back to jobs</a></p>"
    )
    quick_take = as_sentence(content["quick_take"])
    description = quick_take or (
        f"Explore {page_title} at {company_name}, including source details, "
        "requirements, and the official application link."
    )
    if len(description) > 220:
        description = description[:217].rstrip() + "..."

    raw_source_location = candidate_text(job["source_location"])
    variant_label = source_variant_label(job)
    eligibility = candidate_eligibility(
        arrangement,
        raw_source_location,
        variant_label=variant_label,
    )
    source_location = candidate_source_location(raw_source_location)
    eligibility_adds_information = candidate_eligibility_adds_information(
        source_location,
        eligibility,
    )
    eligibility_fact = (
        eligibility["fact"] if eligibility_adds_information else None
    )
    eligibility_details = (
        render_eligibility_details(eligibility)
        if eligibility_adds_information
        else ""
    )
    workplace_label = enum_label(arrangement["workplace_mode"])
    fact_items = unique_pairs_by_value(compact_pairs(
        (
            ("Location", source_location),
            (
                "Where you can work from",
                eligibility_fact,
            ),
            ("Compensation", compensation_label(compensation)),
            (
                "Engagement",
                enum_label(arrangement["engagement_type"])
                or clean(job["source_commitment"]),
            ),
            (
                "Work arrangement",
                workplace_label
                if workplace_adds_information(
                    source_location,
                    eligibility["summary"],
                    workplace_label,
                )
                else None,
            ),
            ("Schedule", enum_label(arrangement["schedule_type"])),
            ("Hours", hours_label(arrangement)),
            ("Duration", clean(arrangement["duration"])),
            ("Apply by", clean(application["deadline"])),
            (
                "Originally posted",
                display_date(original_posted) if original_posted else None,
            ),
            (
                "Application process",
                "Assessment required" if application["assessment_required"] is True else None,
            ),
            (
                "Application process",
                "Work sample required"
                if application["portfolio_or_sample_required"] is True
                else None,
            ),
            (
                "Application process",
                "Account required to apply" if application["login_required"] is True else None,
            ),
        )
    ))
    facts = render_fact_grid(fact_items)

    role_chips = unique_text(
        [
            candidate_chip_value(job["source_department"]),
            clean(job["source_expertise"]),
            enum_label(role["role_family"]),
            enum_label(role["seniority"]),
            *(enum_label(value) for value in role["professional_domains"]),
            *(activity_label(value) for value in role["work_activities"]),
            *role["specializations"],
        ]
    )
    chips = render_chips(role_chips)

    apply_action = ""
    if job["official_url"] and job["job_is_active"] and job["canonical_is_active"]:
        apply_action = (
            f"<a class='button button-primary' href='{e(job['official_url'])}' "
            "target='_blank' rel='noopener noreferrer nofollow'>Apply on company site</a>"
        )

    workflow = render_workflow_panel(
        job,
        authenticated=authenticated,
        controls=workflow_controls,
        status=workflow_status,
    )

    requirement_blocks = []
    if content["candidate_profile"]:
        requirement_blocks.append(
            f"<p class='candidate-profile'>{e(as_sentence(content['candidate_profile']))}</p>"
        )
    requirement_blocks.extend(
        render_labeled_list("Required skills", requirements["skills_required"]),
    )
    requirement_blocks.extend(
        render_labeled_list("Preferred skills", requirements["skills_preferred"]),
    )
    languages = [
        language_label(item)
        for item in candidate_language_requirements(job, requirements)
    ]
    requirement_blocks.extend(render_labeled_list("Languages", languages))
    education = requirements["education"]
    education_values = []
    minimum = enum_label(education["minimum_level"])
    if minimum:
        education_values.append(minimum)
    education_values.extend(enum_label(item) for item in education["accepted_alternatives"])
    requirement_blocks.extend(render_labeled_list("Education", education_values))
    if requirements["years_experience_min"] is not None:
        years = format_number(requirements["years_experience_min"])
        requirement_blocks.append(
            f"<p><strong>Experience:</strong> At least {e(years)} "
            f"{'year' if years == '1' else 'years'}</p>"
        )
    requirement_blocks.extend(render_labeled_list("Credentials", requirements["credentials"]))
    requirement_blocks.extend(render_labeled_list("Licenses", requirements["licenses"]))
    requirements_section = render_content_section(
        "What they're looking for",
        "".join(requirement_blocks),
    )

    about_content = ""
    if quick_take:
        about_content += f"<p>{e(quick_take)}</p>"
    benefits = unique_text(content["benefits"])
    if benefits:
        benefit_items = "".join(f"<li>{e(item)}</li>" for item in benefits)
        about_content += (
            "<div class='section-detail'><h3>What the employer highlights</h3>"
            f"<ul>{benefit_items}</ul></div>"
        )
    about_section = render_content_section(
        "What this opportunity is about",
        about_content,
    )

    caveats = candidate_facing_caveats(content["caveats"])

    source_url = job["official_url"]
    source_link = (
        f"<a href='{e(source_url)}' target='_blank' rel='noopener noreferrer nofollow'>"
        f"{e(company_name)} original listing</a>"
        if source_url
        else e(company_name)
    )
    verified_at = first_timestamp(
        job["last_captured_at"],
        job["latest_successful_source_run_at"],
        job["job_last_seen_at"],
        job["canonical_last_seen_at"],
    )
    freshness = (
        f"<span><strong>Last verified:</strong> {e(display_date(verified_at))}</span>"
        if verified_at
        else ""
    )
    provenance_section = f"""
    <footer class='verification-footer' id='source-and-freshness'>
      <div class='verification-line'><span><strong>Official source:</strong> {source_link}</span>{freshness}</div>
      <p>Company representative? Contact Wahojobs if this listing needs an update or should be removed.</p>
    </footer>
    """

    company_path = job.get("company_path")
    company_link = (
        f"<a href='{e(company_path)}'>{e(company_name)}</a>"
        if company_path
        else e(company_name)
    )

    company_section = ""
    company_url = human_facing_company_url(job["careers_url"])
    if company_path:
        official_company_link = (
            f"<a href='{e(company_url)}' target='_blank' "
            f"rel='noopener noreferrer nofollow'>Visit {e(company_name)} careers</a>"
            if company_url
            else ""
        )
        company_section = f"""
        <section class='company-strip'>
          <h2>More from {e(company_name)}</h2>
          <p><a href='{e(company_path)}'>See current {e(company_name)} opportunities on Wahojobs</a></p>
          {official_company_link}
        </section>
        """

    page = f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{e(page_title)} at {e(company_name)} | Wahojobs</title>
  <meta name='description' content='{e(description)}'>
  {robots}
  <link rel='canonical' href='{e(canonical_url)}'>
  <meta property='og:type' content='website'>
  <meta property='og:title' content='{e(page_title)} at {e(company_name)}'>
  <meta property='og:description' content='{e(description)}'>
  <meta property='og:url' content='{e(canonical_url)}'>
  {jobposting_script}
  <style>{PUBLIC_JOB_CSS}</style>
</head>
<body>
  <header class='site-header'>
    <a class='brand' href='/jobs'>Wahojobs</a>
    {navigation}
  </header>
  <main>
    {back_to_jobs}
    <article>
      <header class='hero'>
        <div class='hero-copy'>
          <p class='eyebrow'>Job opportunity</p>
          <h1>{e(page_title)}</h1>
          <p class='company-line'>{company_link}</p>
          {facts}
          {eligibility_details}
          {chips}
          <div class='hero-actions'>{apply_action}</div>
        </div>
        {workflow}
      </header>
      <div id='action-feedback' aria-live='polite'></div>
      <div class='job-description'>
        {source_description_section}
        {about_section}
        {render_list_section("What you'll do", content['responsibilities'])}
        {requirements_section}
        {render_list_section('Important things to know', caveats, css_class='caveats')}
      </div>
      {company_section}
      {provenance_section}
    </article>
  </main>
  {workflow_script if authenticated else ''}
</body>
</html>"""
    if jobposting_script and len(page.encode("utf-8")) > MAX_PUBLIC_JOB_PAGE_BYTES:
        without_jobposting = dict(job)
        without_jobposting["jobposting_evidence"] = None
        return render_public_job_page(
            without_jobposting,
            public_origin=public_origin,
            authenticated=authenticated,
            navigation=navigation,
            workflow_controls=workflow_controls,
            workflow_status=workflow_status,
            workflow_script=workflow_script,
            catalog_return_to=catalog_return_to,
        )
    return page


def render_unavailable_public_job_page(
    job,
    *,
    public_origin,
    navigation="",
    catalog_return_to=None,
):
    canonical_url = public_origin.rstrip("/") + job["path"]
    title = clean(job.get("source_title")) or clean(job.get("canonical_title"))
    company = clean(job.get("company_name"))
    return_target = safe_catalog_return_target(catalog_return_to) or "/jobs"
    company_path = job.get("company_path")
    company_link = (
        f"<a href='{e(company_path)}'>{e(company)}</a>"
        if company_path
        else e(company)
    )
    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{e(title)} at {e(company)} | Wahojobs</title>
  <meta name='description' content='This opportunity is not currently available on Wahojobs.'>
  <meta name='robots' content='noindex,follow'>
  <link rel='canonical' href='{e(canonical_url)}'>
  <style>{PUBLIC_JOB_CSS}</style>
</head>
<body>
  <header class='site-header'>
    <a class='brand' href='/jobs'>Wahojobs</a>
    {navigation}
  </header>
  <main>
    <p class='back-to-jobs'><a href='{e(return_target)}'>← Back to jobs</a></p>
    <article>
      <header class='hero'>
        <div class='hero-copy'>
          <p class='eyebrow'>Opportunity unavailable</p>
          <h1>{e(title)}</h1>
          <p class='company-line'>{company_link}</p>
          <p>This opportunity is not currently available in trusted public inventory.</p>
          <div class='hero-actions'><a class='button button-primary' href='/jobs'>Browse current jobs</a></div>
        </div>
      </header>
    </article>
  </main>
</body>
</html>"""


def render_workflow_panel(job, *, authenticated, controls, status):
    if not authenticated:
        return """
        <aside class='workflow-card logged-out'>
          <p class='eyebrow'>Make it personal</p>
          <h2>See how this fits you</h2>
          <p>Create a profile to get personalized matches and keep jobs organized in My Jobs.</p>
          <a class='button button-secondary' href='/login'>Create a profile or sign in</a>
        </aside>
        """
    status_html = (
        f"<p class='pill js-card-status' aria-label='Current status: {e(status)}'>{e(status)}</p>"
        if status
        else "<p class='pill js-card-status'></p>"
    )
    return f"""
    <aside class='workflow-card' data-action-card>
      <div class='card-main'>
        <p class='eyebrow'>Your Wahojobs workflow</p>
        <h2>Track this opportunity</h2>
        <p>Use the same workflow as your matches and My Jobs.</p>
        {status_html}
      </div>
      <div class='js-card-controls workflow-controls'>{controls}</div>
    </aside>
    """


def render_fact_grid(items):
    if not items:
        return ""
    cards = "".join(
        f"<div class='fact'><dt>{e(label)}</dt><dd>{e(value)}</dd></div>"
        for label, value in items
    )
    return f"<dl class='fact-grid' aria-label='Key job facts'>{cards}</dl>"


def render_chips(values):
    items = unique_text(values)
    if not items:
        return ""
    rendered = "".join(f"<li>{e(item)}</li>" for item in items)
    return f"<ul class='role-chips' aria-label='Role and activity highlights'>{rendered}</ul>"


def render_text_section(title, text, *, eyebrow=None):
    value = clean(text)
    if not value:
        return ""
    eyebrow_html = f"<p class='eyebrow'>{e(eyebrow)}</p>" if eyebrow else ""
    return f"<section class='section-card'>{eyebrow_html}<h2>{e(title)}</h2><p>{e(value)}</p></section>"


def render_list_section(title, values, *, css_class=""):
    items = unique_text(values)
    if not items:
        return ""
    class_name = "content-section" + (" " + css_class if css_class else "")
    rendered = "".join(f"<li>{e(item)}</li>" for item in items)
    return f"<section class='{e(class_name)}'><h2>{e(title)}</h2><ul>{rendered}</ul></section>"


def render_definition_section(title, intro, items):
    if not items:
        return ""
    intro_html = f"<p class='muted'>{e(intro)}</p>" if intro else ""
    rendered = "".join(
        f"<div><dt>{e(label)}</dt><dd>{e(value)}</dd></div>"
        for label, value in items
    )
    return f"<section class='section-card'><h2>{e(title)}</h2>{intro_html}<dl class='details-grid'>{rendered}</dl></section>"


def render_content_section(title, content):
    if not content:
        return ""
    return f"<section class='content-section'><h2>{e(title)}</h2>{content}</section>"


def render_labeled_list(label, values):
    items = unique_text(values)
    if not items:
        return []
    rendered = "".join(f"<li>{e(value)}</li>" for value in items)
    return [f"<div class='requirement-group'><h3>{e(label)}</h3><ul>{rendered}</ul></div>"]


def candidate_eligibility(arrangement, source_location=None, *, variant_label=None):
    """Build one concise candidate-facing eligibility presentation."""

    source_location = candidate_text(source_location)
    source_scope, source_mode, _requirements, _restriction = classify_job_location(
        source_location
    )
    mode = clean(arrangement.get("workplace_mode"))
    scope = clean(arrangement.get("location_scope"))
    if not mode or mode == "unknown":
        mode = source_mode
    if not scope or scope == "unknown":
        scope = source_scope

    countries = canonical_candidate_countries(
        arrangement.get("eligible_countries") or []
    )
    regions = canonical_candidate_regions(
        arrangement.get("eligible_regions") or []
    )
    if not countries:
        countries = set(countries_in_location(source_location))
    if not regions:
        regions = set(regions_in_location(source_location))
    if not countries:
        countries = set(countries_in_location(candidate_text(variant_label)))
    if not regions:
        regions = set(regions_in_location(candidate_text(variant_label)))

    countries = tuple(sorted(countries, key=str.casefold))
    regions = tuple(sorted(regions, key=str.casefold))
    if scope == LOCATION_SCOPE_REMOTE_WORLDWIDE:
        summary = "Work from anywhere"
        fact = "Worldwide"
    elif regions:
        region_list = natural_join(regions)
        summary = "Eligible across " + region_list
        fact = region_list
    elif len(countries) == 1:
        summary = "Eligible in " + countries[0]
        fact = countries[0]
    elif 1 < len(countries) <= 3:
        country_list = natural_join(countries)
        summary = "Eligible in " + country_list
        fact = country_list
    elif len(countries) == 4:
        summary = f"Eligible in {len(countries)} countries"
        fact = natural_join(countries)
    elif len(countries) > 4:
        summary = f"Eligible in {len(countries)} countries"
        fact = f"{len(countries)} countries"
    elif selected_locations_placeholder(source_location) or any(
        selected_locations_placeholder(value)
        for value in arrangement.get("eligible_locations") or []
    ):
        summary = "Available in selected locations"
        fact = summary
    else:
        source_without_remote = re.sub(
            r"^remote\s*(?:[-—:]\s*)?",
            "",
            source_location or "",
            flags=re.IGNORECASE,
        ).strip(" ;,|:-")
        if source_without_remote and len(source_without_remote) <= 80:
            summary = "Eligible in " + source_without_remote
            fact = source_without_remote
        elif scope == LOCATION_SCOPE_REMOTE_RESTRICTED:
            summary = "Location restrictions apply"
            fact = summary
        else:
            summary = None
            fact = None

    return {
        "scope": scope,
        "mode": mode,
        "countries": countries,
        "regions": regions,
        "summary": summary,
        "fact": fact,
        "detail_countries": countries if len(countries) > 4 else (),
    }


def candidate_eligibility_adds_information(source_location, eligibility):
    if not eligibility["summary"]:
        return False
    source_location = candidate_text(source_location)
    if not source_location:
        return True
    if eligibility["scope"] == LOCATION_SCOPE_REMOTE_WORLDWIDE:
        words = normalized_words(source_location)
        return not bool({"world", "worldwide", "anywhere"} & words)

    source_countries = set(countries_in_location(source_location))
    eligible_countries = set(eligibility["countries"])
    if eligible_countries:
        return eligible_countries != source_countries
    source_regions = set(regions_in_location(source_location))
    eligible_regions = set(eligibility["regions"])
    if eligible_regions:
        return eligible_regions != source_regions
    return normalize_candidate_words(source_location) != normalize_candidate_words(
        eligibility["summary"]
    )


def candidate_source_location(value):
    value = candidate_text(value)
    return None if selected_locations_placeholder(value) else value


def selected_locations_placeholder(value):
    words = normalize_candidate_words(value)
    return words == "selected locations" or words.endswith(" selected locations")


def source_variant_label(job):
    if clean(job.get("rich_source_type")) != "oneforma-wordpress-marketplace":
        return None
    raw = job.get("rich_metadata_json")
    if isinstance(raw, dict):
        metadata = raw
    else:
        try:
            metadata = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return None
    if not isinstance(metadata, dict):
        return None
    return candidate_text(metadata.get("variant_language"))


def source_variant_language(job):
    variant_label = source_variant_label(job)
    mentions = find_language_mentions(variant_label or "")
    return candidate_language_display(mentions[0]["language"]) if mentions else None


def candidate_language_requirements(job, requirements):
    variant_language = source_variant_language(job)
    if variant_language:
        return [
            {
                "language": variant_language,
                "locale": None,
                "requirement_mode": "single",
            }
        ]

    result = []
    seen = set()
    for item in requirements.get("languages") or []:
        if not isinstance(item, dict):
            continue
        mode = clean(item.get("requirement_mode")).casefold()
        if mode in {"", "none", "ambiguous", "unknown"}:
            continue
        language = candidate_language_display(item.get("language"))
        if not language or language.casefold() in seen:
            continue
        seen.add(language.casefold())
        result.append(
            {
                "language": language,
                "locale": candidate_text(item.get("locale")),
                "requirement_mode": mode,
            }
        )
    return result


def candidate_language_display(value):
    value = candidate_text(value)
    if not value:
        return None
    return value.title() if value == value.casefold() else value


def render_eligibility_details(eligibility):
    countries = eligibility["detail_countries"]
    if not countries:
        return ""
    count = len(countries)
    items = "".join(f"<li>{e(country)}</li>" for country in countries)
    return (
        "<details class='eligibility-details'>"
        f"<summary>See all {count} eligible countries</summary>"
        f"<ul>{items}</ul>"
        "</details>"
    )


def canonical_candidate_countries(values):
    countries = set()
    for value in values:
        country = canonical_country_from_text(value)
        if country:
            countries.add(country)
    return countries


def canonical_candidate_regions(values):
    known = {
        value.casefold(): value
        for value in (REGION_AMERICAS, REGION_EMEA, REGION_APAC)
    }
    return {
        known[value.casefold()]
        for raw in values
        if (value := clean(raw)) and value.casefold() in known
    }


def natural_join(values):
    values = list(values)
    if len(values) < 2:
        return "".join(values)
    if len(values) == 2:
        return values[0] + " and " + values[1]
    return ", ".join(values[:-1]) + ", and " + values[-1]


def candidate_text(value):
    value = clean(value)
    return None if not value or normalize_candidate_words(value) == "unknown" else value


def normalize_candidate_words(value):
    return " ".join(re.findall(r"[a-z0-9]+", (clean(value) or "").casefold()))


def workplace_adds_information(source_location, eligibility_label, workplace_label):
    if not workplace_label:
        return False
    if workplace_label.casefold() != "remote":
        return True
    return "remote" not in normalized_words(source_location) and not eligibility_label


def normalized_words(value):
    return set(re.findall(r"[a-z0-9]+", (clean(value) or "").casefold()))


def candidate_chip_value(value):
    value = clean(value)
    if not value or ">" in value:
        return None
    return value


def activity_label(value):
    value = clean(value)
    friendly = {
        "ai_training_evaluation": "AI training & evaluation",
        "research_analysis": "Research & analysis",
        "software_development": "Software development",
        "writing_editing": "Writing & editing",
    }
    return friendly.get(value, enum_label(value))


def compensation_label(compensation):
    if compensation.get("disclosed") is not True:
        return None
    minimum = compensation.get("amount_min")
    maximum = compensation.get("amount_max")
    currency = clean(compensation.get("currency"))
    period = enum_label(compensation.get("period"))
    amount_type = compensation.get("amount_type")
    if minimum is None and maximum is None:
        return clean(compensation.get("notes"))
    minimum_label = money_label(minimum, currency) if minimum is not None else None
    maximum_label = money_label(maximum, currency) if maximum is not None else None
    if amount_type == "from" and minimum_label:
        amount = "From " + minimum_label
    elif amount_type == "up_to" and maximum_label:
        amount = "Up to " + maximum_label
    elif minimum_label and maximum_label and minimum_label != maximum_label:
        amount = minimum_label + "–" + maximum_label
    else:
        amount = minimum_label or maximum_label
    if amount and period:
        amount += " per " + period.lower()
    notes = clean(compensation.get("notes"))
    return " — ".join(value for value in (amount, notes) if value)


def hours_label(arrangement):
    minimum = arrangement.get("hours_per_week_min")
    maximum = arrangement.get("hours_per_week_max")
    if minimum is None and maximum is None:
        return None
    if minimum is not None and maximum is not None and minimum != maximum:
        return f"{format_number(minimum)}–{format_number(maximum)} hours per week"
    value = minimum if minimum is not None else maximum
    return f"{format_number(value)} hours per week"


def language_label(item):
    if not isinstance(item, dict):
        return None
    language = candidate_language_display(item.get("language"))
    locale = candidate_text(item.get("locale"))
    mode = clean(item.get("requirement_mode")).casefold()
    if not language:
        return None
    result = language + (f" ({locale})" if locale else "")
    mode_label = {
        "all_required": "All required",
        "any_supported": "Any supported",
    }.get(mode)
    return result + (f" — {mode_label}" if mode_label else "")


def enum_label(value):
    value = clean(value)
    if not value or value == "unknown":
        return None
    label = value.replace("_", " ").replace("-", " ").title()
    for ordinary, acronym in (("Ai", "AI"), ("Api", "API"), ("Llm", "LLM"), ("Qa", "QA"), ("Sme", "SME")):
        label = re.sub(rf"\b{ordinary}\b", acronym, label)
    return label


def join_labels(values):
    return ", ".join(filter(None, (enum_label(value) for value in values or []))) or None


def join_text(values):
    return ", ".join(unique_text(values)) or None


def compact_pairs(items):
    return [(label, value) for label, value in items if clean(value)]


def unique_pairs_by_value(items):
    result = []
    seen = set()
    for label, value in items:
        key = clean(value).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append((label, value))
    return result


def unique_text(values):
    result = []
    seen = set()
    for value in values or []:
        value = clean(value)
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def candidate_facing_caveats(values):
    result = []
    technical_terms = (
        "metadata",
        "schema field",
        "taxonomy field",
        "enrichment field",
        "source packet",
        "field evidence",
    )
    for value in unique_text(values):
        value = re.sub(
            r"\s*\((?:source\s+)?metadata field\)\s*",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        value = re.sub(r"\s+([.,;:])", r"\1", value)
        if not value or any(term in value.casefold() for term in technical_terms):
            continue
        result.append(as_sentence(value))
    return unique_text(result)


def as_sentence(value):
    value = clean(value)
    if not value:
        return None
    return value if value.endswith((".", "!", "?")) else value + "."


def money_label(value, currency):
    amount = format_number(value)
    return f"{currency} {amount}" if currency else amount


def format_number(value):
    number = float(value)
    return f"{number:,.0f}" if number.is_integer() else f"{number:,.2f}".rstrip("0").rstrip(".")


def first_safe_url(*values):
    for value in values:
        value = clean(value)
        if not value:
            continue
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme in {"http", "https"}
                and parsed.netloc
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
                and not any(character.isspace() for character in parsed.netloc)
            ):
                parsed.port
                return value
        except (TypeError, ValueError):
            continue
    return None


def human_facing_company_url(value):
    value = first_safe_url(value)
    if not value:
        return None
    parsed = urlsplit(value)
    host_labels = (parsed.hostname or "").casefold().split(".")
    if any(
        label == "api" or label.startswith("api-") or label.endswith("-api")
        for label in host_labels
    ):
        return None
    path = parsed.path.casefold()
    if any(marker in path for marker in ("/api/", "/v1/", "/v2/", "/v3/", "/wp-json/")):
        return None
    query = parsed.query.casefold()
    if "mode=json" in query or "format=json" in query:
        return None
    return value


def first_human_facing_url(*values):
    for value in values:
        safe = human_facing_company_url(value)
        if safe:
            return safe
    return None


def first_timestamp(*values):
    for value in values:
        value = clean(value)
        if value:
            return value
    return None


def display_timestamp(value):
    value = clean(value)
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    date_label = f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
    return f"{date_label} at {parsed.strftime('%H:%M')} UTC" if parsed.tzinfo else date_label


def display_date(value):
    value = clean(value)
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def safe_catalog_return_target(value):
    value = clean(value)
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.path != "/jobs"
        or parsed.fragment
    ):
        return None
    return value


def e(value):
    return html.escape(str(value or ""), quote=True)


PUBLIC_JOB_CSS = """
:root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; color: #18231e; background: #f3f6f4; }
* { box-sizing: border-box; }
body { margin: 0; line-height: 1.6; }
a { color: #146149; font-weight: 700; }
.site-header { width: min(1120px, calc(100% - 32px)); margin: 0 auto; min-height: 72px; display: flex; align-items: center; justify-content: space-between; gap: 24px; }
.brand { color: #13271f; font-size: 1.25rem; text-decoration: none; }
.account-nav { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 16px; }
main { width: min(1120px, calc(100% - 32px)); margin: 0 auto; padding: 8px 0 48px; }
.back-to-jobs { margin: 0 0 12px 2px; }
.hero { display: grid; grid-template-columns: minmax(0, 1.65fr) minmax(280px, .75fr); gap: 24px; align-items: start; background: #fff; border: 1px solid #d9e2dd; border-radius: 18px; padding: clamp(24px, 5vw, 52px); box-shadow: 0 14px 40px rgba(28, 53, 42, .07); }
.eyebrow { margin: 0 0 8px; color: #527064; font-size: .78rem; font-weight: 800; letter-spacing: .09em; text-transform: uppercase; }
h1 { margin: 0; max-width: 780px; font-size: clamp(2rem, 5vw, 3.65rem); line-height: 1.04; letter-spacing: -.035em; }
h2 { margin: 0 0 12px; font-size: clamp(1.35rem, 2.5vw, 1.8rem); line-height: 1.2; }
h3 { margin: 18px 0 6px; font-size: 1rem; }
p { margin: 0 0 14px; }
.company-line { margin: 14px 0 22px; color: #3e564b; font-size: 1.22rem; font-weight: 750; }
.fact-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 0 0 24px; }
.fact { background: #f5f8f6; border-radius: 10px; padding: 11px 13px; }
.fact dt { color: #5c6d65; font-size: .78rem; font-weight: 800; letter-spacing: .04em; text-transform: uppercase; }
.fact dd { margin: 2px 0 0; font-weight: 700; }
.eligibility-details { margin: -12px 0 24px; border: 1px solid #d6e3dc; border-radius: 10px; background: #fbfcfb; padding: 10px 13px; }
.eligibility-details summary { color: #146149; font-weight: 750; cursor: pointer; }
.eligibility-details ul { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px 18px; margin: 10px 0 2px; padding-left: 20px; }
.role-chips { display: flex; flex-wrap: wrap; gap: 8px; margin: -4px 0 24px; padding: 0; list-style: none; }
.role-chips li { border: 1px solid #d6e3dc; border-radius: 999px; background: #f7faf8; color: #355447; padding: 5px 10px; font-size: .86rem; font-weight: 700; }
.hero-actions { display: flex; flex-wrap: wrap; gap: 12px; }
.button { display: inline-flex; justify-content: center; align-items: center; border-radius: 9px; padding: 11px 17px; text-decoration: none; }
.button-primary { background: #176b52; color: #fff; }
.button-secondary { background: #e2f0e9; color: #164f3e; }
.workflow-card { border: 1px solid #cbdad2; border-radius: 14px; padding: 20px; background: #f5faf7; }
.workflow-card h2 { font-size: 1.25rem; }
.workflow-controls, .workflow-controls form { display: grid; gap: 8px; }
.workflow-controls button { width: 100%; border: 1px solid #b7c9bf; border-radius: 8px; background: #fff; color: #164f3e; padding: 10px 12px; font: inherit; font-weight: 750; cursor: pointer; }
.workflow-controls input { display: none; }
.pill { display: inline-block; min-height: 0; margin: 0 0 12px; border-radius: 999px; background: #dfeee7; color: #164f3e; padding: 5px 10px; font-size: .85rem; font-weight: 800; }
.pill:empty { display: none; }
.job-description { margin-top: 20px; border: 1px solid #d9e2dd; border-radius: 14px; background: #fff; padding: 0 clamp(22px, 4vw, 38px); }
.content-section { padding: clamp(26px, 4vw, 38px) 0; }
.content-section + .content-section { border-top: 1px solid #e2e9e5; }
.content-section ul { margin: 8px 0 0; padding-left: 22px; }
.content-section li + li { margin-top: 7px; }
.section-detail { margin-top: 24px; }
.candidate-profile { color: #304b3f; font-size: 1.05rem; }
.requirement-group + .requirement-group { margin-top: 22px; }
.muted { color: #5c6d65; }
.caveats { color: #543e2e; }
.company-strip { display: grid; grid-template-columns: minmax(180px, .6fr) minmax(0, 1.4fr) auto; gap: 20px; align-items: center; margin-top: 20px; border: 1px solid #d9e2dd; border-radius: 12px; background: #f8faf9; padding: 20px 24px; }
.company-strip h2, .company-strip p { margin: 0; }
.verification-footer { width: auto; margin: 24px 0 0; border-top: 1px solid #d2ddd7; padding: 20px 4px 8px; color: #5c6d65; font-size: .9rem; }
.verification-line { display: flex; flex-wrap: wrap; gap: 10px 24px; margin-bottom: 8px; }
.verification-footer p { max-width: 850px; margin: 0; }
.notice { margin: 16px 0; border-radius: 9px; padding: 12px 14px; }
.notice.warning { background: #fff2dc; color: #6d4314; }
.notice.success, .action-feedback.success { background: #e2f4ea; color: #14543d; }
.notice.error, .action-feedback.error { background: #fde8e5; color: #7a281d; }
.action-feedback { margin-top: 12px; border-radius: 8px; padding: 10px; }
@media (max-width: 780px) { .hero { grid-template-columns: 1fr; } .fact-grid { grid-template-columns: 1fr; } .eligibility-details ul { grid-template-columns: repeat(2, minmax(0, 1fr)); } .company-strip { grid-template-columns: 1fr; gap: 8px; } .site-header { align-items: flex-start; padding: 18px 0; } }
"""


__all__ = [
    "PUBLIC_JOB_PATH",
    "PUBLIC_JOB_STATE_LIVE",
    "PUBLIC_JOB_STATE_TEMPORARILY_UNAVAILABLE",
    "load_public_job",
    "parse_public_job_path",
    "public_company_path",
    "public_historical_inventory_is_eligible",
    "public_inventory_is_eligible",
    "public_job_classification_is_eligible",
    "public_opportunity_is_eligible",
    "public_job_path",
    "public_job_path_for_match",
    "public_job_slug",
    "representative_variant_rank",
    "render_public_job_page",
]

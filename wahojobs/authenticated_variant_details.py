"""Keep authenticated recommendation navigation bound to a source variant.

Canonical grouping is not proof that regional postings share eligibility.
This module changes navigation/presentation only, never admission or ranking.
"""
from html import escape
import re
from urllib.parse import parse_qs, urlencode

from wahojobs import public_job_page, public_jobs_catalog


def variant_detail_url(match, *, run_id=None):
    path = public_job_page.public_job_path_for_match(match)
    job_id = match.get("job_id")
    if path is None or type(job_id) is not int or not 0 < job_id <= public_job_page.MAX_SQLITE_INTEGER:
        return None
    params = {"variant": job_id}
    if run_id is not None:
        params["run"] = run_id
    return path + "?" + urlencode(params)


def parse_variant_query(query):
    if not query:
        return {}
    try:
        raw = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
    except (UnicodeError, ValueError):
        return None
    if not set(raw) <= {"variant", "run", "return_to"} or any(len(v) != 1 for v in raw.values()):
        return None
    result = {}
    if "variant" in raw:
        value = raw["variant"][0]
        if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > public_job_page.MAX_SQLITE_INTEGER:
            return None
        result["variant"] = int(value)
    if "run" in raw:
        from wahojobs.authenticated_profile_matches import _MATCH_RUN_REFERENCE
        value = raw["run"][0]
        if "variant" not in result or _MATCH_RUN_REFERENCE.fullmatch(value) is None:
            return None
        result["run"] = value
    if "return_to" in raw:
        value = public_jobs_catalog.validate_catalog_return_target(raw["return_to"][0])
        if value is None:
            return None
        result["return_to"] = value
    return result


def find_presented_variant(context, canonical_id, job_id=None):
    from wahojobs.authenticated_profile_matches import (
        _primary_presentation_matches, _presented_relaxation_scenarios,
    )
    groups = [(_primary_presentation_matches(context), "main")]
    groups.extend((scenario["matches"], "relaxation")
                  for scenario in _presented_relaxation_scenarios(context))
    for matches, section in groups:
        candidates = [m for m in matches if m.get("canonical_opportunity_id") == canonical_id
                      and (job_id is None or m.get("job_id") == job_id)]
        if len(candidates) > 1:
            raise ValueError("ambiguous_recommendation_variant")
        if candidates:
            return dict(candidates[0], _detail_recommendation_section=section)
    return None


def prepare_variant_notice(job, match):
    if match is not None and (
        match.get("job_id") != job["job_id"]
        or match.get("canonical_opportunity_id") != job["canonical_opportunity_id"]
        or match.get("url") != job["official_url"]
    ):
        raise ValueError("recommendation_source_variant_mismatch")
    job["_authenticated_recommendation"] = match


def append_variant_notice(content, job):
    from wahojobs.authenticated_profile_matches import _candidate_match_explanations
    match = job.get("_authenticated_recommendation")
    current = match is not None and job["public_state"] == public_job_page.PUBLIC_JOB_STATE_LIVE
    if current:
        status = ("This is the source variant shown in your preference-relaxation preview."
                  if match.get("_detail_recommendation_section") == "relaxation" else
                  "This is the source variant shown in your current matches.")
        location_status = match.get("location_eligibility_status", "unknown")
        location = ("Applicant-location compatibility is supported by the stored evidence. "
                    "This does not establish other qualifications."
                    if location_status == "eligible" else
                    "Applicant-location eligibility remains unresolved. Remote work does not establish worldwide eligibility.")
        reasons = "".join("<li>" + escape(reason) + "</li>"
                          for reason in _candidate_match_explanations(match))
    else:
        status = "This source variant is not in your current recommendations."
        location = "Review the source conditions and your current matches before applying."
        reasons = ""
        # An old recommendation link remains useful as a source record, but must
        # not keep its old recommendation/apply presentation after invalidation.
        content = content.replace(
            "Apply on company site</a>", "View source listing</a>")
    source = escape(str(job.get("external_id") or job["job_id"]))
    section = (
        "<section class='content-section' aria-label='Recommendation source'>"
        f"<p><strong>{escape(status)}</strong></p><p>{escape(location)}</p>"
        f"<p>Source record: {source}</p>"
        + (f"<ul>{reasons}</ul>" if reasons else "")
        + "<p>Application acceptance has not been verified by this view.</p>"
        "<p><a href='/find-matches'>Return to current matches</a></p></section>"
    )
    return content.replace("<div class='job-description'>", "<div class='job-description'>" + section, 1)

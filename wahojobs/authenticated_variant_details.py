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
        _recommendation_presentation_matches, _presented_relaxation_scenarios,
    )
    # Membership is the actually displayed bounded union, not the sum of two
    # independently capped internal routes. Scoped local eligibility is separate.
    groups = [(_recommendation_presentation_matches(context), "recommendation")]
    groups.extend((scenario["matches"], "relaxation")
                  for scenario in _presented_relaxation_scenarios(context))
    for matches, section in groups:
        candidates = [m for m in matches if m.get("canonical_opportunity_id") == canonical_id
                      and (job_id is None or m.get("job_id") == job_id)]
        if len(candidates) > 1:
            raise ValueError("ambiguous_recommendation_variant")
        if candidates:
            if section == "recommendation":
                section = ("conditional" if candidates[0].get('conditional_task_fit') is True
                           and candidates[0].get('primary_recommendation_eligible') is False else "main")
            return dict(candidates[0], _detail_recommendation_section=section)
    return None


def load_scoped_snapshot(connection, canonical_id, requested_id, *, now):
    """Read only one canonical group, including exact source/detail identities.

    The caller owns a short read transaction. Scoring and HTML rendering happen
    after it is released; even a concurrent commit cannot mix these copies.
    """
    import sqlite3
    from scripts import profile_to_matches_preview as preview
    from wahojobs.matching.source_geography import apply_mercor_applicant_geography
    from wahojobs.matching.recommendation_validity import database_commit_token
    from wahojobs.authenticated_card_evidence import load_card_sources
    rows = preview.query_preview_rows(connection, canonical_opportunity_id=canonical_id)
    rows = apply_mercor_applicant_geography(connection, rows)
    detail_evidence = public_job_page.load_public_job_evidence(
        connection, public_job_page.public_job_path(canonical_id))
    # Admission's scoped rows already contain the authoritative observation
    # selection (including validated per-record provenance and its fallback).
    # Reuse those inputs for detail trust; do not substitute source-wide dates
    # or interpret a capture/HTTP success as a new availability observation.
    if detail_evidence is not None:
        availability = {row['job_id']: row for row in rows}
        if len(availability) != len(rows):
            raise ValueError('ambiguous_scoped_availability')
        for detail in detail_evidence['rows']:
            row = availability.get(detail['job_id'])
            if row is None:
                # Inactive/historical records keep the existing unavailable path.
                continue
            if (row['canonical_opportunity_id'] != detail['canonical_opportunity_id']
                    or row['source_slug'] != detail['company_slug']
                    or row['url'] != detail['listing_url']):
                raise ValueError('scoped_availability_identity_mismatch')
            for field in ('source_run_id', 'source_run_started_at',
                          'latest_successful_source_run_at', 'source_run_qualifies'):
                detail[field] = row[field]
    effective = ((detail_evidence or {}).get("effective"))
    effective = {canonical_id: effective} if effective is not None else {}
    try:
        token = database_commit_token(connection)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        # No token means unproven membership, never a reason to compute a list.
        token = None
    return {"rows": rows, "detail_evidence": detail_evidence, "effective": effective, "token": token,
            "task_sources": load_card_sources(connection, rows)}


def resolve_scoped_variant(snapshot, profile_v2, overlay, requested_id, *, now, background_context=None):
    """Reuse production scoring, gates and representative logic on this group.

    The returned local checks are NOT a recommendation context or list proof.
    """
    from wahojobs import authenticated_profile_matches as browser
    from wahojobs.matching.metadata_overlay import apply_overlay_to_rows
    from wahojobs.profiles.canonical_v2 import project_v2_to_matcher_v1
    preview = browser.profile_preview
    rows = apply_overlay_to_rows(snapshot["rows"], overlay)
    evaluated = []
    if rows:
        projected = project_v2_to_matcher_v1(profile_v2, matcher_profile_id="scoped-job-detail")
        preview.build_preview_context_from_canonical_rows(
            projected, inventory_rows=rows, metadata_overlay_status={},
            limit=max(1, len(rows)), evaluated_at=now,
            evaluated_match_sink=evaluated.append)
    from wahojobs.matching.source_task_fit import apply_source_task_fit
    evaluated = [apply_source_task_fit(m, snapshot.get("task_sources", {}).get(m["job_id"]), profile_v2,
                                       background_context=background_context)
                 for m in evaluated]
    evaluated = browser._mark_variant_preference_admission(
        evaluated, profile_v2, rows, snapshot['effective'])
    by_id = {match["job_id"]: match for match in evaluated}
    if len(by_id) != len(evaluated):
        raise ValueError("ambiguous_scoped_variant")
    if requested_id is None and evaluated:
        representatives = preview.dedupe_matches(evaluated)
        if len(representatives) != 1:
            raise ValueError("ambiguous_scoped_canonical")
        requested_id = representatives[0]["job_id"]
    job = public_job_page.prepare_public_job(
        snapshot["detail_evidence"], selected_job_id=requested_id, now=now)
    if job is not None:
        if requested_id is None:
            # Historical canonical navigation still scopes facts to its source.
            job = public_job_page.prepare_public_job(
                snapshot["detail_evidence"], selected_job_id=job["job_id"], now=now)
        requested_id = job["job_id"]
    match = by_id.get(requested_id)
    local = {"match": match, "passes": False, "preference_evaluations": []}
    if match is not None:
        if background_context is not None:
            from wahojobs.authenticated_card_evidence import prepare_card_evidence
            from wahojobs.professional_background_semantics import digest
            local["background_card_evidence"] = prepare_card_evidence(match,
                snapshot.get("task_sources", {}).get(requested_id), profile_v2,
                include_item_experience=True, background_context=background_context)
            local["background_profile_digest"] = digest(profile_v2)
        single = {"matches": {match["preview_section"]: [match]}}
        if browser._has_authoritative_preference_model(profile_v2):
            single = browser._apply_typed_preference_enforcement_v1(
                profile_v2, single, rows, snapshot["effective"])
        # Enforcement records the actual outcomes on copied matches, including
        # a rejected variant. Detail guidance must use that evaluated copy.
        local["match"] = next((item for items in single["matches"].values() for item in items
                               if item["job_id"] == requested_id), match)
        local["passes"] = bool(browser._recommendation_presentation_matches(single))
        local["preference_evaluations"] = single.get("_typed_preference_enforcement", {}).get("evaluations", [])
    return job, local


def prepare_variant_notice(job, match, *, local=None, membership_known=False):
    for evidence in (match, (local or {}).get("match")):
        if evidence is not None and (
            evidence.get("job_id") != job["job_id"]
            or evidence.get("canonical_opportunity_id") != job["canonical_opportunity_id"]
            or evidence.get("url") != job["official_url"]
        ):
            raise ValueError("recommendation_source_variant_mismatch")
    job["_authenticated_recommendation"] = match
    job["_authenticated_local_checks"] = local or {}
    job["_authenticated_membership_known"] = membership_known


def append_variant_notice(content, job):
    from wahojobs.authenticated_profile_matches import _candidate_match_explanations
    match = job.get("_authenticated_recommendation")
    local = job.get("_authenticated_local_checks") or {}
    evidence = local.get("match") or match or {}
    current = match is not None and job["public_state"] == public_job_page.PUBLIC_JOB_STATE_LIVE
    if current:
        status = ("This is the source variant shown in your preference-relaxation preview."
                  if match.get("_detail_recommendation_section") == "relaxation" else
                  "This is the source variant shown in your current matches.")
    elif job.get("_authenticated_membership_known"):
        status = "This source variant is not in your current recommendations."
    else:
        status = "Current recommendation-list membership has not been established for this source variant."
    if local.get("passes"):
        checks = "This variant passes the current local eligibility and preference checks. This does not prove recommendation-list membership or all qualifications."
    elif evidence:
        checks = "This variant does not pass the current local recommendation checks. Missing evidence is not a proven incompatibility."
    else:
        checks = "Current candidate eligibility has not been established for this source record."
    location_status = evidence.get("location_eligibility_status", "unknown")
    if location_status == "eligible":
        location = "Applicant-location compatibility is supported by the stored evidence. This does not establish other qualifications."
    elif location_status == "incompatible":
        location = "The source applicant-location restriction conflicts with your current profile location."
    else:
        location = "Applicant-location eligibility remains unresolved. Remote work does not establish worldwide eligibility."
    if job["public_state"] != public_job_page.PUBLIC_JOB_STATE_LIVE:
        checks = "This source record is not currently available under the existing availability checks."
    observation = escape(str(job.get('latest_successful_source_run_at') or 'not established'))
    availability = (
        "Catalog availability passes the existing verification checks."
        if job['public_state'] == public_job_page.PUBLIC_JOB_STATE_LIVE else
        "Catalog availability is not current under the existing verification checks."
    )
    reasons = "".join("<li>" + escape(reason) + "</li>"
                      for reason in _candidate_match_explanations(match or evidence)
                      if current or local.get("passes"))
    if not current:
        content = content.replace("Apply on company site</a>", "View source listing</a>")
    # The public template's timestamp may be a content capture, not the
    # qualifying availability observation. Keep those claims separate here.
    content = content.replace('<strong>Last verified:</strong>', '<strong>Source record timestamp:</strong>')
    source = escape(str(job.get("external_id") or job["job_id"]))
    section = (
        "<section class='content-section' aria-label='Recommendation source'>"
        f"<p><strong>{escape(status)}</strong></p><p>{escape(checks)}</p><p>{escape(location)}</p>"
        f"<p>{availability} Qualifying observation: {observation}. "
        "This does not establish an active vacancy, project or application acceptance.</p>"
        f"<p>Source record: {source}</p>"
        + (f"<ul>{reasons}</ul>" if reasons else "")
        + "<p>Application acceptance has not been verified by this view.</p>"
        "<p><a href='/find-matches'>Return to current matches</a></p></section>"
    )
    if "<div class='job-description'>" in content:
        return content.replace("<div class='job-description'>", "<div class='job-description'>" + section, 1)
    return content.replace("</article>", section + "</article>", 1)

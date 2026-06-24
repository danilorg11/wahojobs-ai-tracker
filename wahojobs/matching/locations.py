from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


LOCATION_ELIGIBLE = "eligible"
LOCATION_INCOMPATIBLE = "incompatible"
LOCATION_UNKNOWN = "unknown"
LOCATION_NOT_APPLICABLE = "not_applicable"

PROFILE_LOCATION_KNOWN = "known"
PROFILE_LOCATION_UNKNOWN = "unknown"

LOCATION_SCOPE_REMOTE_WORLDWIDE = "remote_worldwide"
LOCATION_SCOPE_REMOTE_RESTRICTED = "remote_restricted"
LOCATION_SCOPE_RESTRICTED = "onsite_or_hybrid_restricted"
LOCATION_SCOPE_UNKNOWN = "unknown"

RESTRICTION_NONE = "none"
RESTRICTION_CONCRETE = "concrete"
RESTRICTION_OPAQUE = "opaque"

REMOTE_STATUS_REMOTE = "remote"
REMOTE_STATUS_HYBRID = "hybrid"
REMOTE_STATUS_ONSITE = "onsite"
REMOTE_STATUS_UNKNOWN = "unknown"

LOCATION_UNCONFIRMED_REASON = "Location eligibility could not be confirmed for this opportunity."

LOCATION_COUNTRY_TERMS = {
    "argentina",
    "australia",
    "austria",
    "belgium",
    "brazil",
    "canada",
    "china",
    "france",
    "germany",
    "india",
    "ireland",
    "italy",
    "japan",
    "mexico",
    "morocco",
    "netherlands",
    "new zealand",
    "portugal",
    "spain",
    "united kingdom",
    "uk",
    "united states",
    "united states of america",
    "usa",
    "us",
}


@dataclass(frozen=True)
class LocationEligibility:
    status: str
    reason: str
    profile_location: str
    profile_location_status: str
    applicant_location_requirements: str
    restriction_type: str
    job_location_scope: str
    job_remote_status: str
    actionability_cap_required: bool


def location_eligibility(profile: dict, row: dict) -> LocationEligibility:
    profile_location = explicit_profile_location(profile)
    profile_location_status = PROFILE_LOCATION_KNOWN if profile_location else PROFILE_LOCATION_UNKNOWN
    scope, remote_status, requirements, restriction_type = classify_job_location(row.get("location"))

    if scope == LOCATION_SCOPE_REMOTE_WORLDWIDE:
        return LocationEligibility(
            status=LOCATION_NOT_APPLICABLE,
            reason="Stored location indicates remote worldwide/global availability.",
            profile_location=profile_location,
            profile_location_status=profile_location_status,
            applicant_location_requirements=requirements,
            restriction_type=restriction_type,
            job_location_scope=scope,
            job_remote_status=remote_status,
            actionability_cap_required=False,
        )

    if not requirements:
        return LocationEligibility(
            status=LOCATION_UNKNOWN,
            reason="Stored data does not provide enough applicant-location evidence.",
            profile_location=profile_location,
            profile_location_status=profile_location_status,
            applicant_location_requirements=requirements,
            restriction_type=restriction_type,
            job_location_scope=scope,
            job_remote_status=remote_status,
            actionability_cap_required=False,
        )

    if restriction_type == RESTRICTION_OPAQUE:
        return LocationEligibility(
            status=LOCATION_UNKNOWN,
            reason=LOCATION_UNCONFIRMED_REASON,
            profile_location=profile_location,
            profile_location_status=profile_location_status,
            applicant_location_requirements=requirements,
            restriction_type=restriction_type,
            job_location_scope=scope,
            job_remote_status=remote_status,
            actionability_cap_required=True,
        )

    if not profile_location:
        return LocationEligibility(
            status=LOCATION_UNKNOWN,
            reason=LOCATION_UNCONFIRMED_REASON,
            profile_location=profile_location,
            profile_location_status=profile_location_status,
            applicant_location_requirements=requirements,
            restriction_type=restriction_type,
            job_location_scope=scope,
            job_remote_status=remote_status,
            actionability_cap_required=True,
        )

    requirement_text = normalize_location_text(requirements)
    profile_text = normalize_location_text(profile_location)
    if profile_text and (profile_text in requirement_text or requirement_text in profile_text):
        return LocationEligibility(
            status=LOCATION_ELIGIBLE,
            reason="Profile location appears in the stored applicant-location requirement.",
            profile_location=profile_location,
            profile_location_status=profile_location_status,
            applicant_location_requirements=requirements,
            restriction_type=restriction_type,
            job_location_scope=scope,
            job_remote_status=remote_status,
            actionability_cap_required=False,
        )

    return LocationEligibility(
        status=LOCATION_INCOMPATIBLE,
        reason="Stored profile location does not appear to match the applicant-location requirement.",
        profile_location=profile_location,
        profile_location_status=profile_location_status,
        applicant_location_requirements=requirements,
        restriction_type=restriction_type,
        job_location_scope=scope,
        job_remote_status=remote_status,
        actionability_cap_required=False,
    )


def explicit_profile_location(profile: dict) -> str:
    for key in ("location", "country", "residence", "city", "region"):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def classify_job_location(location: str | None) -> tuple[str, str, str, str]:
    text = normalize_location_text(location)
    if not text or text in {"unknown", "not specified", "n/a", "none"}:
        return LOCATION_SCOPE_UNKNOWN, REMOTE_STATUS_UNKNOWN, "", RESTRICTION_NONE

    remote_status = REMOTE_STATUS_UNKNOWN
    if "hybrid" in text:
        remote_status = REMOTE_STATUS_HYBRID
    elif "onsite" in text or "on site" in text:
        remote_status = REMOTE_STATUS_ONSITE
    elif "remote" in text or "work from home" in text:
        remote_status = REMOTE_STATUS_REMOTE

    worldwide = any(term in text for term in ("worldwide", "world wide", "global", "anywhere"))
    restriction_type = location_restriction_type(text)
    restricted = restriction_type != RESTRICTION_NONE
    raw_location = str(location or "").strip()

    if worldwide and not restricted:
        return LOCATION_SCOPE_REMOTE_WORLDWIDE, remote_status, "", RESTRICTION_NONE
    if remote_status == REMOTE_STATUS_REMOTE and worldwide:
        return LOCATION_SCOPE_REMOTE_WORLDWIDE, remote_status, "", RESTRICTION_NONE
    if remote_status == REMOTE_STATUS_REMOTE and restricted:
        return LOCATION_SCOPE_REMOTE_RESTRICTED, remote_status, raw_location, restriction_type
    if remote_status in {REMOTE_STATUS_HYBRID, REMOTE_STATUS_ONSITE}:
        if restriction_type == RESTRICTION_NONE:
            restriction_type = RESTRICTION_OPAQUE
        return LOCATION_SCOPE_RESTRICTED, remote_status, raw_location, restriction_type
    if remote_status == REMOTE_STATUS_UNKNOWN and restricted:
        return LOCATION_SCOPE_RESTRICTED, remote_status, raw_location, restriction_type
    return LOCATION_SCOPE_UNKNOWN, remote_status, "", RESTRICTION_NONE


def has_location_restriction(text: str) -> bool:
    return location_restriction_type(text) != RESTRICTION_NONE


def location_restriction_type(text: str) -> str:
    if any(term in text for term in ("selected locations", "specific locations")):
        return RESTRICTION_OPAQUE
    if "must be based" in text:
        return RESTRICTION_OPAQUE
    if " - " in text or "," in text:
        return RESTRICTION_CONCRETE if any(term in text for term in LOCATION_COUNTRY_TERMS) else RESTRICTION_NONE
    if any(re.search(rf"\b{re.escape(term)}\b", text) for term in LOCATION_COUNTRY_TERMS):
        return RESTRICTION_CONCRETE
    return RESTRICTION_NONE


def normalize_location_text(value: str | None) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[\u2010-\u2015]", " - ", text)
    text = text.replace("/", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
import unicodedata

from wahojobs.profiles.countries import CANONICAL_COUNTRIES, COUNTRY_BY_CODE, normalize_country


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

REGION_AMERICAS = "Americas"
REGION_EMEA = "EMEA"
REGION_APAC = "APAC"
REGIONAL_LOCATION_TOKENS = {
    REGION_AMERICAS: ("americas", "amer"),
    REGION_EMEA: ("emea",),
    REGION_APAC: ("apac",),
}
REGION_COUNTRY_CODES = {
    REGION_AMERICAS: frozenset(
        "AG AI AR AW BB BL BM BO BQ BR BS BZ CA CL CO CR CU CW DM DO EC FK GF GD GL GP GT GY HN HT JM KN KY LC MF MQ MS MX NI PA PE PM PR PY SR SV SX TC TT US UY VC VE VG VI".split()
    ),
    REGION_EMEA: frozenset(
        "AD AE AF AL AM AO AT AX AZ BA BE BF BG BH BI BJ BW BY CD CF CG CH CI CM CV CY CZ DE DJ DK DZ EE EG EH ER ES ET FI FO FR GA GB GE GG GH GI GM GN GQ GR HR HU IE IL IM IQ IR IS IT JE JO KE KG KM KW KZ LB LI LR LS LT LU LV LY MA MC MD ME MG MK ML MR MT MU MW MZ NA NE NG NL NO OM PL PS PT QA RE RO RS RU RW SA SC SD SE SH SI SK SL SM SO SS ST SY SZ TD TG TJ TM TN TR TZ UA UG UZ VA XK YE YT ZA ZM ZW".split()
    ),
    REGION_APAC: frozenset(
        "AS AU BD BN BT CC CK CN CX FJ FM GU HK ID IN IO JP KH KI KP KR LA LK MM MN MO MP MV MY NC NF NP NR NU NZ PF PG PH PK PN PW SB SG TH TK TL TO TV TW VN VU WF WS".split()
    ),
}
COUNTRY_REGION = {
    COUNTRY_BY_CODE[code]: region
    for region, codes in REGION_COUNTRY_CODES.items()
    for code in codes
    if code in COUNTRY_BY_CODE
}
COUNTRY_PATTERNS = tuple(
    (
        country,
        re.compile(
            rf"\b{re.escape(' '.join(unicodedata.normalize('NFKD', country.casefold()).encode('ascii', 'ignore').decode('ascii').split()))}\b"
        ),
    )
    for country in sorted(CANONICAL_COUNTRIES, key=lambda value: (-len(value), value))
    if len(country) > 2
)
COUNTRY_ALIAS_PATTERNS = (
    (re.compile(r"\bunited states of america\b"), "United States"),
    (re.compile(r"\bunited states\b"), "United States"),
    (re.compile(r"\busa\b"), "United States"),
    (re.compile(r"\bus\b"), "United States"),
    (re.compile(r"\bunited kingdom\b"), "United Kingdom"),
    (re.compile(r"\buk\b"), "United Kingdom"),
)
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
    stored_requirements = row.get("applicant_location_requirements") or row.get("location")
    scope, remote_status, requirements, restriction_type = classify_job_location(stored_requirements)

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
    profile_country = canonical_country_from_text(profile_location)
    required_countries = countries_in_location(requirements)
    required_regions = regions_in_location(requirements)
    region_match = bool(
        profile_country
        and COUNTRY_REGION.get(profile_country) in required_regions
    )
    country_match = bool(profile_country and profile_country in required_countries)
    textual_match = bool(
        not required_countries
        and not required_regions
        and profile_text
        and (profile_text in requirement_text or requirement_text in profile_text)
    )
    if country_match or region_match or textual_match:
        return LocationEligibility(
            status=LOCATION_ELIGIBLE,
            reason="Profile country is included in the stored country or regional requirement.",
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

    # Multiple stored locations form a union. An explicit Global/Worldwide
    # option therefore grants broad eligibility even when narrower options are
    # listed beside it; generic Remote still does not.
    if worldwide:
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
    if countries_in_location(text) or regions_in_location(text):
        return RESTRICTION_CONCRETE
    if "must be based" in text:
        return RESTRICTION_OPAQUE
    return RESTRICTION_NONE


@lru_cache(maxsize=8192)
def regions_in_location(value: str | None) -> frozenset[str]:
    text = normalize_location_text(value)
    found = set()
    for region, tokens in REGIONAL_LOCATION_TOKENS.items():
        if any(re.search(rf"\b{re.escape(token)}\b", text) for token in tokens):
            found.add(region)
    return frozenset(found)


@lru_cache(maxsize=8192)
def countries_in_location(value: str | None) -> frozenset[str]:
    text = normalize_location_text(value)
    found = set()
    for pattern, country in COUNTRY_ALIAS_PATTERNS:
        if pattern.search(text):
            found.add(country)
    for country, pattern in COUNTRY_PATTERNS:
        if pattern.search(text):
            found.add(country)
    return frozenset(found)


@lru_cache(maxsize=4096)
def canonical_country_from_text(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return normalize_country(text)
    except ValueError:
        countries = countries_in_location(text)
        return next(iter(countries)) if len(countries) == 1 else ""


@lru_cache(maxsize=8192)
def normalize_location_text(value: str | None) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[\u2010-\u2015]", " - ", text)
    text = text.replace("/", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()

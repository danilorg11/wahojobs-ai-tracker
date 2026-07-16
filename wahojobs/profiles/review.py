"""Reviewed canonical-profile updates for the local product flow."""

from __future__ import annotations

from copy import deepcopy

from wahojobs.profiles.canonical import (
    AVAILABILITY_STATUSES,
    EMPLOYMENT_TYPES,
    PHONE_PREFERENCES,
    PROFILE_SOURCE_USER_CONFIRMATION,
    PROFILE_SOURCE_USER_CORRECTION,
    SCHEDULE_PREFERENCES,
    SYNCHRONOUS_PREFERENCES,
    UNKNOWN,
    canonical_to_matcher_profile,
    field_sources_for_profile,
    unique_strings,
    validate_canonical_profile,
)
from wahojobs.profiles.countries import normalize_country
from wahojobs.profiles.normalizer import signals_for_domains


LANGUAGE_PROFICIENCIES = (
    "native",
    "fluent",
    "professional",
    "intermediate",
    "basic",
    "unspecified",
)

EDUCATION_LEVELS = (
    "not_specified",
    "no_degree",
    "high_school",
    "associate",
    "bachelor",
    "master",
    "doctorate",
    "professional_degree",
)

CREDENTIAL_STATUSES = (UNKNOWN, "absent", "explicit", "in_progress")


def apply_reviewed_profile(canonical_profile, updates):
    """Apply one explicit review form as the authoritative profile state."""
    validate_canonical_profile(canonical_profile)
    original = deepcopy(canonical_profile)
    canonical = deepcopy(canonical_profile)

    location = canonical["location"]
    country = normalize_country(text(updates.get("country")), allow_missing=True)
    eligible_countries = [
        normalize_country(value)
        for value in string_list(updates.get("eligible_countries"))
    ]
    location.update(
        {
            "country": country,
            "region": text(updates.get("region")),
            "city": text(updates.get("city")),
            "residence": country,
            "work_authorization": text(updates.get("work_authorization")) or UNKNOWN,
            "eligible_countries": eligible_countries,
            "remote_eligibility": "explicit" if updates.get("remote") else "unknown",
            "restrictions": string_list(updates.get("geographic_restrictions")),
            "geographic_work_restrictions": string_list(
                updates.get("geographic_restrictions")
            ),
        }
    )

    canonical["languages"] = reviewed_languages(updates.get("languages") or [])

    education = canonical["education"]
    level = text(updates.get("education_level")) or "not_specified"
    if level not in EDUCATION_LEVELS:
        raise ValueError("Choose a supported education level.")
    education.update(
        {
            "education_level": level,
            "degrees": string_list(updates.get("degrees")),
            "fields_or_domains": string_list(updates.get("education_fields")),
            "institutions": string_list(updates.get("institutions")),
            "completion_status": text(updates.get("education_status")) or UNKNOWN,
        }
    )

    credentials = canonical["credentials"]
    credential_status = text(updates.get("credential_status")) or UNKNOWN
    if credential_status not in CREDENTIAL_STATUSES:
        raise ValueError("Choose a supported credential status.")
    credentials.update(
        {
            "certifications": string_list(updates.get("certifications")),
            "licenses": string_list(updates.get("licenses")),
            "jurisdictions": string_list(updates.get("jurisdictions")),
            "security_clearances": string_list(updates.get("security_clearances")),
            "credential_status": credential_status,
        }
    )

    experience = canonical["experience"]
    experience.update(
        {
            "total_years": optional_years(updates.get("total_years")),
            "seniority": text(updates.get("seniority")) or UNKNOWN,
            "recent_roles": string_list(updates.get("job_titles")),
            "job_titles": string_list(updates.get("job_titles")),
            "occupational_families": string_list(
                updates.get("occupational_families")
            ),
            "professional_domains": string_list(updates.get("professional_domains")),
            "industries": string_list(updates.get("industries")),
            "contribution_type": text(updates.get("contribution_type")) or UNKNOWN,
            "specialties": string_list(updates.get("specialties")),
        }
    )

    skills = string_list(updates.get("skills"))
    canonical["skills"] = {
        "normalized": skills,
        "free_text_labels": skills,
        "entries": [
            {
                "skill": skill,
                "evidence": [],
                "confidence": "high",
                "provenance": PROFILE_SOURCE_USER_CORRECTION,
            }
            for skill in skills
        ],
        "technical": string_list(updates.get("technical_skills")),
        "software_tools": string_list(updates.get("software_tools")),
        "writing_research": string_list(updates.get("writing_research_skills")),
        "administrative_support": string_list(
            updates.get("administrative_support_skills")
        ),
        "domain_specific": string_list(updates.get("domain_specific_skills")),
    }

    preferences = canonical["preferences"]
    employment_types = reviewed_enum_list(
        updates.get("employment_types"), EMPLOYMENT_TYPES, "employment type"
    )
    target_types = string_list(updates.get("target_opportunity_types"))
    remote = bool(updates.get("remote"))
    flexible = bool(updates.get("flexible"))
    preferences.update(
        {
            "remote": remote,
            "flexible": flexible,
            "employment_types": employment_types,
            "synchronous_preference": reviewed_enum(
                updates.get("synchronous_preference"),
                SYNCHRONOUS_PREFERENCES,
                "synchronous preference",
            ),
            "phone_preference": reviewed_enum(
                updates.get("phone_preference"), PHONE_PREFERENCES, "phone preference"
            ),
            "schedule": reviewed_enum_list(
                updates.get("schedule"), SCHEDULE_PREFERENCES, "schedule preference"
            ),
            "availability": reviewed_enum(
                updates.get("availability"), AVAILABILITY_STATUSES, "availability"
            ),
            "target_opportunity_types": target_types,
            "preferred_task_types": target_types,
            "work_preferences": unique_strings(
                employment_types
                + (["remote"] if remote else [])
                + (["flexible"] if flexible else [])
            ),
        }
    )

    constraints = canonical["constraints"]
    hard = string_list(updates.get("hard_constraints"))
    if updates.get("no_degree"):
        hard.append("no college degree")
    if updates.get("no_experience"):
        hard.append("no prior experience")
    if updates.get("no_specialized_credentials"):
        hard.append("no specialized credentials")
    excluded_domains = string_list(updates.get("excluded_domains"))
    accessibility = string_list(updates.get("accessibility_constraints"))
    constraints.update(
        {
            "hard_constraints": unique_strings(hard),
            "soft_preferences": string_list(updates.get("soft_preferences")),
            "avoid_keywords": unique_strings(
                string_list(updates.get("avoid_keywords")) + excluded_domains
            ),
            "negative_constraints": unique_strings(excluded_domains + accessibility),
            "excluded_domains": excluded_domains,
            "accessibility_constraints": accessibility,
        }
    )

    domains = unique_strings(
        education["fields_or_domains"] + experience["professional_domains"]
    )
    signals = signals_for_domains(domains, skills, canonical["languages"])
    canonical["derived_matcher_signals"] = {
        "signals": signals,
        "derived_domains": domains,
        "derived_target_work_types": target_types,
        "avoid_keywords": constraints["avoid_keywords"],
    }
    provenance = canonical["provenance"]
    provenance["reviewed"] = True
    provenance["missing_fields"] = reviewed_missing_fields(canonical)
    provenance["ambiguous_fields"] = []
    existing_sources = {
        path: detail
        for path, detail in (provenance.get("field_sources") or {}).items()
        if path.startswith("identity.")
    }
    confirmed_sources = field_sources_for_profile(
        canonical,
        PROFILE_SOURCE_USER_CONFIRMATION,
        explicit=True,
    )
    changed_roots = {
        root
        for root in (
            "languages",
            "location",
            "education",
            "credentials",
            "experience",
            "skills",
            "preferences",
            "constraints",
        )
        if canonical.get(root) != original.get(root)
    }
    reviewed_sources = {
        path: (
            {"source": PROFILE_SOURCE_USER_CORRECTION, "explicit": True}
            if path.split(".", 1)[0].split("[", 1)[0] in changed_roots
            else detail
        )
        for path, detail in confirmed_sources.items()
    }
    existing_sources.update(
        {
            path: detail
            for path, detail in reviewed_sources.items()
            if not path.startswith("identity.")
        }
    )
    provenance["field_sources"] = existing_sources

    canonical["matcher_compatible_profile"] = {}
    canonical["matcher_compatible_profile"] = canonical_to_matcher_profile(canonical)
    validate_canonical_profile(canonical)
    return canonical


def reviewed_languages(rows):
    result = []
    seen = set()
    for row in rows:
        language = text(row.get("language"))
        if not language:
            continue
        key = language.casefold()
        if key in seen:
            raise ValueError(
                f"List {language} only once and choose one proficiency."
            )
        seen.add(key)
        proficiency = text(row.get("proficiency")) or "unspecified"
        if proficiency not in LANGUAGE_PROFICIENCIES:
            raise ValueError(f"Choose a supported proficiency for {language}.")
        result.append(
            {
                "language": language,
                "proficiency": proficiency,
                "locale": text(row.get("locale")),
                "evidence": [],
                "confidence": "high",
                "proficiency_explicit": True,
                "provenance": PROFILE_SOURCE_USER_CORRECTION,
            }
        )
    return result


def reviewed_missing_fields(canonical):
    missing = []
    if not canonical["languages"]:
        missing.append("languages")
    if not canonical["location"].get("country"):
        missing.append("location")
    if canonical["credentials"].get("credential_status") == UNKNOWN:
        missing.extend(["certifications", "licenses"])
    if canonical["experience"].get("total_years") is None:
        missing.append("total_years")
    return missing


def optional_years(value):
    value = text(value)
    if not value:
        return None
    try:
        years = int(value)
    except ValueError as exc:
        raise ValueError("Years of experience must be a whole number.") from exc
    if years < 0 or years > 80:
        raise ValueError("Years of experience must be between 0 and 80.")
    return years


def reviewed_enum(value, allowed, label):
    value = text(value) or UNKNOWN
    by_key = {item.casefold(): item for item in allowed}
    canonical = by_key.get(value.casefold())
    if canonical is None:
        raise ValueError(f"Choose a supported {label}.")
    return canonical


def reviewed_enum_list(value, allowed, label):
    return [reviewed_enum(item, allowed, label) for item in string_list(value)]


def string_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        values = value.replace("\r", "\n").replace(";", ",").replace("\n", ",").split(",")
    else:
        values = value
    return unique_strings(values)


def text(value):
    if value is None:
        return ""
    return str(value).strip()

"""Canonical Profile Schema V1 and adapters.

This module defines a versioned profile contract that can later receive facts
from resumes, LinkedIn-style profiles, paragraphs, or sparse self-descriptions.
For now it only adapts the existing matcher profile dictionaries without
changing matcher behavior.
"""

from __future__ import annotations

from copy import deepcopy
import json
import re

from wahojobs.profiles.countries import is_canonical_country, normalize_country


SCHEMA_VERSION = "canonical_profile_v1"
UNKNOWN = "unknown"
ABSENT = "absent"

PROFILE_SOURCE_EXPLICIT_USER = "explicit_user_entry"
PROFILE_SOURCE_PARSED_TEXT = "parsed_free_text"
PROFILE_SOURCE_RESUME = "resume_extraction"
PROFILE_SOURCE_EXTERNAL = "external_import"
PROFILE_SOURCE_USER_CORRECTION = "user_correction"
PROFILE_SOURCE_USER_CONFIRMATION = "user_confirmation"
PROFILE_SOURCES = {
    PROFILE_SOURCE_EXPLICIT_USER,
    PROFILE_SOURCE_PARSED_TEXT,
    PROFILE_SOURCE_RESUME,
    PROFILE_SOURCE_EXTERNAL,
    PROFILE_SOURCE_USER_CORRECTION,
    PROFILE_SOURCE_USER_CONFIRMATION,
}

LANGUAGE_PROFICIENCIES = {
    UNKNOWN,
    "native",
    "fluent",
    "professional",
    "intermediate",
    "basic",
    "unspecified",
    "advanced",
    "conversational",
    "reading",
}
CONFIDENCE_LEVELS = {UNKNOWN, "low", "medium", "high"}
EDUCATION_LEVELS = {
    "not_specified",
    "no_degree",
    "high_school",
    "associate",
    "bachelor",
    "master",
    "doctorate",
    "professional_degree",
    # Legacy matcher vocabulary remains valid at the adapter boundary.
    "advanced_degree",
    "professional",
    "technical",
    "phd",
}
EDUCATION_COMPLETION_STATUSES = {UNKNOWN, "not_specified", "in_progress", "completed"}
CREDENTIAL_STATUSES = {UNKNOWN, ABSENT, "explicit", "in_progress"}
REMOTE_ELIGIBILITY_STATUSES = {UNKNOWN, ABSENT, "explicit"}
EMPLOYMENT_TYPES = {
    "full-time",
    "part-time",
    "freelance",
    "contract",
    "temporary",
    "seasonal",
    "internship",
    "entry-level",
    "flexible",
}
SYNCHRONOUS_PREFERENCES = {
    UNKNOWN,
    "no preference",
    "synchronous",
    "asynchronous",
    "flexible",
}
PHONE_PREFERENCES = {
    UNKNOWN,
    "no preference",
    "phone acceptable",
    "phone preferred",
    "non-phone preferred",
    "non-phone required",
}
SCHEDULE_PREFERENCES = {
    "full-time",
    "part-time",
    "flexible",
    "synchronous",
    "asynchronous",
    "weekdays",
    "weekends",
    "evenings",
    "business hours",
}
AVAILABILITY_STATUSES = {
    UNKNOWN,
    "immediate",
    "available",
    "limited",
    "unavailable",
    "full-time",
    "part-time",
}
SENIORITY_LEVELS = {
    UNKNOWN,
    "student",
    "entry-level",
    "junior",
    "mid",
    "mid-level",
    "senior",
    "lead",
    "principal",
    "executive",
    "advanced",
}
CONTRIBUTION_TYPES = {
    UNKNOWN,
    "individual contributor",
    "manager",
    "management",
    "executive",
}

MATCHER_PROFILE_FIELDS = (
    "profile_id",
    "display_name",
    "summary",
    "education_level",
    "degrees_or_domains",
    "languages",
    "skills",
    "work_preferences",
    "constraints",
    "target_opportunity_types",
    "notes",
    "signals",
    "avoid_keywords",
    "location",
    "country",
    "residence",
    "city",
    "region",
)


def matcher_profile_to_canonical(profile, *, source_inputs=None, extracted_from="matcher_profile"):
    """Convert the current matcher profile dict to Canonical Profile Schema V1."""
    source_inputs = list(source_inputs or [])
    signals = [signal_to_canonical(signal) for signal in list_value(profile, "signals")]
    languages = [
        {
            "language": language,
            "proficiency": profile.get("language_proficiency", {}).get(language, UNKNOWN)
            if isinstance(profile.get("language_proficiency"), dict)
            else UNKNOWN,
            "locale": "",
            "evidence": [],
            "confidence": UNKNOWN,
        }
        for language in list_value(profile, "languages")
    ]
    work_preferences = list_value(profile, "work_preferences")
    degrees_or_domains = list_value(profile, "degrees_or_domains")
    skills = list_value(profile, "skills")
    target_types = list_value(profile, "target_opportunity_types")
    constraints = list_value(profile, "constraints")
    avoid_keywords = list_value(profile, "avoid_keywords")

    raw_country = string_value(profile, "country")
    country = normalize_country(raw_country, allow_missing=True)
    raw_residence = string_value(profile, "residence")
    residence = normalize_country(raw_residence, allow_missing=True) if raw_residence else country
    canonical = {
        "schema_version": SCHEMA_VERSION,
        "identity": {
            "profile_id": string_value(profile, "profile_id"),
            "display_name": string_value(profile, "display_name"),
            "source_inputs": source_inputs,
        },
        "languages": languages,
        "location": {
            "country": country,
            "region": string_value(profile, "region"),
            "city": string_value(profile, "city"),
            "timezone": string_value(profile, "timezone"),
            "residence": residence,
            "work_authorization": UNKNOWN,
            "eligible_countries": [],
            "remote_eligibility": preference_status(work_preferences, "remote"),
            "restrictions": [],
            "geographic_work_restrictions": [],
        },
        "education": {
            "education_level": string_value(profile, "education_level") or "not_specified",
            "degrees": list_value(profile, "degrees"),
            "fields_or_domains": degrees_or_domains,
            "institutions": list_value(profile, "institutions"),
            "graduation_years": list_value(profile, "graduation_years"),
            "completion_status": string_value(profile, "education_status") or UNKNOWN,
        },
        "credentials": {
            "certifications": list_value(profile, "certifications"),
            "licenses": list_value(profile, "licenses"),
            "jurisdictions": list_value(profile, "jurisdictions"),
            "security_clearances": list_value(profile, "security_clearances"),
            "credential_status": profile.get("credential_status") or UNKNOWN,
        },
        "experience": {
            "total_years": profile.get("total_years"),
            "years_by_domain": dict(profile.get("years_by_domain") or {}),
            "seniority": string_value(profile, "seniority") or UNKNOWN,
            "recent_roles": list_value(profile, "recent_roles"),
            "occupational_families": list_value(profile, "occupational_families"),
            "job_titles": list_value(profile, "recent_roles"),
            "professional_domains": degrees_or_domains,
            "industries": list_value(profile, "industries"),
            "contribution_type": string_value(profile, "contribution_type") or UNKNOWN,
            "specialties": list_value(profile, "specialties"),
        },
        "skills": {
            "normalized": skills,
            "free_text_labels": skills,
            "entries": [
                {
                    "skill": skill,
                    "evidence": [],
                    "confidence": UNKNOWN,
                }
                for skill in skills
            ],
            "technical": list_value(profile, "technical_skills"),
            "software_tools": list_value(profile, "software_tools"),
            "writing_research": list_value(profile, "writing_research_skills"),
            "administrative_support": list_value(profile, "administrative_support_skills"),
            "domain_specific": skills,
        },
        "preferences": {
            "remote": "remote" in normalized_set(work_preferences),
            "flexible": "flexible" in normalized_set(work_preferences),
            "employment_types": employment_types(work_preferences),
            "synchronous_preference": string_value(profile, "synchronous_preference") or UNKNOWN,
            "phone_preference": string_value(profile, "phone_preference") or UNKNOWN,
            "schedule": list_value(profile, "schedule"),
            "availability": string_value(profile, "availability") or UNKNOWN,
            "rate_pay_preference": string_value(profile, "rate_pay_preference"),
            "target_opportunity_types": target_types,
            "preferred_task_types": target_types,
            "work_preferences": work_preferences,
        },
        "constraints": {
            "hard_constraints": constraints,
            "soft_preferences": [],
            "avoid_keywords": avoid_keywords,
            "negative_constraints": list_value(profile, "negative_constraints"),
            "excluded_domains": list_value(profile, "excluded_domains"),
            "accessibility_constraints": list_value(profile, "accessibility_constraints"),
        },
        "derived_matcher_signals": {
            "signals": signals,
            "derived_domains": degrees_or_domains,
            "derived_target_work_types": target_types,
            "avoid_keywords": avoid_keywords,
        },
        "matcher_compatible_profile": {
            field: deepcopy(profile.get(field, default_matcher_field_value(field)))
            for field in MATCHER_PROFILE_FIELDS
        },
        "provenance": {
            "extracted_from": extracted_from,
            "evidence_snippets": list_value(profile, "evidence_snippets"),
            "confidence": profile.get("confidence") or UNKNOWN,
            "missing_fields": missing_fields_for_profile(profile),
            "ambiguous_fields": list_value(profile, "ambiguous_fields"),
        },
    }
    source = (
        extracted_from
        if extracted_from in PROFILE_SOURCES
        else PROFILE_SOURCE_EXTERNAL
    )
    canonical["provenance"]["field_sources"] = field_sources_for_profile(
        canonical,
        source,
        explicit=source in {PROFILE_SOURCE_EXPLICIT_USER, PROFILE_SOURCE_USER_CORRECTION},
    )
    validate_canonical_profile(canonical)
    return canonical


def canonical_to_matcher_profile(canonical_profile):
    """Project reviewed canonical fields into the legacy matcher contract.

    The embedded matcher-compatible block is retained for fixture compatibility
    and diagnostics, but is not authoritative. This projection prevents original
    free text or stale inferred fields from overriding reviewed values.
    """
    validate_canonical_profile(canonical_profile)
    if (
        not canonical_profile.get("provenance", {}).get("reviewed")
        and canonical_profile.get("matcher_compatible_profile")
    ):
        return legacy_matcher_profile(canonical_profile)
    identity = canonical_profile["identity"]
    location = canonical_profile["location"]
    education = canonical_profile["education"]
    experience = canonical_profile["experience"]
    skills = canonical_profile["skills"]
    preferences = canonical_profile["preferences"]
    constraints = canonical_profile["constraints"]
    domains = unique_strings(
        list(education.get("fields_or_domains") or [])
        + list(experience.get("professional_domains") or [])
    )
    work_preferences = unique_strings(
        list(preferences.get("work_preferences") or [])
        + list(preferences.get("employment_types") or [])
        + (["remote"] if preferences.get("remote") is True else [])
        + (["flexible"] if preferences.get("flexible") is True else [])
    )
    hard_constraints = list(constraints.get("hard_constraints") or [])
    soft_preferences = list(constraints.get("soft_preferences") or [])
    matcher_profile = {
        "profile_id": identity["profile_id"],
        "display_name": identity["display_name"],
        "summary": canonical_profile_matcher_text(canonical_profile),
        "education_level": str(education.get("education_level") or "not_specified"),
        "degrees_or_domains": domains,
        "languages": [
            str(language.get("language") or "").strip()
            for language in canonical_profile["languages"]
            if str(language.get("language") or "").strip()
        ],
        "language_proficiency": {
            str(language.get("language") or "").strip(): str(
                language.get("proficiency") or UNKNOWN
            )
            for language in canonical_profile["languages"]
            if str(language.get("language") or "").strip()
        },
        "skills": unique_strings(skills.get("normalized") or []),
        "work_preferences": work_preferences,
        "constraints": unique_strings(hard_constraints + soft_preferences),
        "target_opportunity_types": unique_strings(
            preferences.get("target_opportunity_types") or []
        ),
        "notes": "",
        "avoid_keywords": unique_strings(constraints.get("avoid_keywords") or []),
        "signals": [
        signal_from_canonical(signal)
        for signal in canonical_profile["derived_matcher_signals"]["signals"]
        ],
        "location": str(location.get("country") or location.get("residence") or ""),
        "country": str(location.get("country") or ""),
        "residence": str(location.get("residence") or ""),
        "city": str(location.get("city") or ""),
        "region": str(location.get("region") or ""),
        "recent_roles": unique_strings(experience.get("recent_roles") or []),
        "specialties": unique_strings(experience.get("specialties") or []),
        "total_years": experience.get("total_years"),
        "seniority": str(experience.get("seniority") or UNKNOWN),
        "certifications": unique_strings(
            canonical_profile["credentials"].get("certifications") or []
        ),
        "licenses": unique_strings(
            canonical_profile["credentials"].get("licenses") or []
        ),
        "credential_status": str(
            canonical_profile["credentials"].get("credential_status") or UNKNOWN
        ),
        "phone_preference": str(preferences.get("phone_preference") or UNKNOWN),
        "availability": str(preferences.get("availability") or UNKNOWN),
        "schedule": unique_strings(preferences.get("schedule") or []),
        "negative_constraints": unique_strings(
            constraints.get("negative_constraints") or []
        ),
    }
    return matcher_profile


def legacy_matcher_profile(canonical_profile):
    """Preserve pre-review fixture and CLI matching behavior exactly."""
    matcher_profile = deepcopy(canonical_profile.get("matcher_compatible_profile") or {})
    identity = canonical_profile["identity"]
    matcher_profile["profile_id"] = identity["profile_id"]
    matcher_profile["display_name"] = identity["display_name"]
    matcher_profile["summary"] = string_value(matcher_profile, "summary")
    matcher_profile["education_level"] = (
        string_value(matcher_profile, "education_level") or "not_specified"
    )
    for field in (
        "degrees_or_domains",
        "languages",
        "skills",
        "work_preferences",
        "constraints",
        "target_opportunity_types",
        "avoid_keywords",
    ):
        matcher_profile[field] = list_value(matcher_profile, field)
    matcher_profile["notes"] = string_value(matcher_profile, "notes")
    matcher_profile["signals"] = [
        signal_from_canonical(signal)
        for signal in canonical_profile["derived_matcher_signals"]["signals"]
    ]
    for field in ("location", "country", "residence", "city", "region"):
        matcher_profile[field] = string_value(matcher_profile, field)
    return matcher_profile


def canonical_profile_matcher_text(canonical_profile):
    """Return deterministic matching text composed only from affirmative evidence."""
    blocks = [
        canonical_profile["identity"].get("display_name"),
        canonical_profile["location"].get("country"),
        canonical_profile["location"].get("region"),
        canonical_profile["location"].get("city"),
        canonical_profile["education"].get("education_level"),
        *(canonical_profile["education"].get("degrees") or []),
        *(canonical_profile["education"].get("fields_or_domains") or []),
        *(canonical_profile["experience"].get("recent_roles") or []),
        *(canonical_profile["experience"].get("occupational_families") or []),
        *(canonical_profile["experience"].get("job_titles") or []),
        *(canonical_profile["experience"].get("professional_domains") or []),
        *(canonical_profile["experience"].get("industries") or []),
        *(canonical_profile["experience"].get("specialties") or []),
        *(canonical_profile["skills"].get("normalized") or []),
        *(canonical_profile["preferences"].get("target_opportunity_types") or []),
        *(canonical_profile["preferences"].get("preferred_task_types") or []),
        *(canonical_profile["preferences"].get("work_preferences") or []),
    ]
    for language in canonical_profile["languages"]:
        blocks.extend(
            [
                language.get("language"),
                language.get("locale"),
                language.get("proficiency"),
            ]
        )
    if canonical_profile["preferences"].get("remote") is True:
        blocks.append("remote")
    if canonical_profile["preferences"].get("flexible") is True:
        blocks.append("flexible")
    years = canonical_profile["experience"].get("total_years")
    if years not in (None, ""):
        blocks.append(f"{years} years experience")
    return ". ".join(unique_strings(blocks))


def canonical_profile_fingerprint(canonical_profile):
    """Stable JSON representation suitable for process-local match caching."""
    validate_canonical_profile(canonical_profile)
    return json.dumps(canonical_profile, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def field_sources_for_profile(canonical_profile, source, *, explicit):
    """Describe provenance for every populated material canonical field."""
    if source not in PROFILE_SOURCES:
        raise ValueError(f"Unsupported profile provenance source: {source}")
    result = {}
    excluded_roots = {"derived_matcher_signals", "matcher_compatible_profile", "provenance"}

    def visit(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                visit(child, child_path)
            return
        if isinstance(value, list):
            if not value:
                return
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")
            return
        if value in (None, "", [], {}):
            return
        result[path] = {"source": source, "explicit": bool(explicit)}

    for root, value in canonical_profile.items():
        if root in excluded_roots or root == "schema_version":
            continue
        visit(value, root)
    return result


def complete_trusted_fixture_provenance(canonical_profile):
    """Complete legacy fixture provenance without accepting client paths."""
    completed = deepcopy(canonical_profile)
    provenance = completed.get("provenance")
    if type(provenance) is dict and "field_sources" not in provenance:
        provenance["field_sources"] = field_sources_for_profile(
            completed,
            PROFILE_SOURCE_EXTERNAL,
            explicit=True,
        )
    return completed


def validate_canonical_profile(canonical_profile):
    errors = canonical_profile_errors(canonical_profile)
    if errors:
        raise ValueError("; ".join(errors))
    return True


def canonical_profile_errors(canonical_profile):
    if type(canonical_profile) is not dict:
        return ["canonical profile must be an object"]
    errors = []
    required_roots = {
        "schema_version",
        "identity",
        "languages",
        "location",
        "education",
        "credentials",
        "experience",
        "skills",
        "preferences",
        "constraints",
        "derived_matcher_signals",
        "matcher_compatible_profile",
        "provenance",
    }
    reject_object_keys(canonical_profile, required_roots, "canonical profile", errors)
    for field in sorted(required_roots - set(canonical_profile)):
        errors.append(f"canonical profile is missing {field}")
    if canonical_profile.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")

    validate_identity(canonical_profile.get("identity"), errors)
    validate_languages(canonical_profile.get("languages"), errors)
    validate_location(canonical_profile.get("location"), errors)
    validate_education(canonical_profile.get("education"), errors)
    validate_credentials(canonical_profile.get("credentials"), errors)
    validate_experience(canonical_profile.get("experience"), errors)
    validate_skills(canonical_profile.get("skills"), errors)
    validate_preferences(canonical_profile.get("preferences"), errors)
    validate_constraints(canonical_profile.get("constraints"), errors)
    validate_derived_signals(canonical_profile.get("derived_matcher_signals"), errors)
    validate_matcher_compatible(canonical_profile.get("matcher_compatible_profile"), errors)
    validate_provenance(canonical_profile, errors)
    validate_profile_contradictions(canonical_profile, errors)
    return errors


def reject_object_keys(value, allowed, path, errors):
    if type(value) is not dict:
        errors.append(f"{path} must be an object")
        return False
    unknown = sorted(set(value) - set(allowed))
    for key in unknown:
        errors.append(f"{path}.{key} is not supported")
    return True


def require_string(value, path, errors, *, nonempty=False, normalized=False):
    if type(value) is not str:
        errors.append(f"{path} must be a string")
        return
    if normalized and value != value.strip():
        errors.append(f"{path} must not have leading or trailing whitespace")
    if nonempty and not value:
        errors.append(f"{path} must be a non-empty string")


def validate_string_list(value, path, errors, *, unique=True):
    if type(value) is not list:
        errors.append(f"{path} must be a list")
        return
    seen = set()
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        require_string(item, item_path, errors, nonempty=True, normalized=True)
        if type(item) is not str:
            continue
        key = item.casefold()
        if unique and key in seen:
            errors.append(f"{path} must not contain duplicate values")
        seen.add(key)


def validate_identity(identity, errors):
    if not reject_object_keys(identity, {"profile_id", "display_name", "source_inputs"}, "identity", errors):
        return
    require_string(identity.get("profile_id"), "identity.profile_id", errors, nonempty=True, normalized=True)
    require_string(identity.get("display_name"), "identity.display_name", errors, nonempty=True, normalized=True)
    source_inputs = identity.get("source_inputs")
    if type(source_inputs) is not list:
        errors.append("identity.source_inputs must be a list")
        return
    for index, source_input in enumerate(source_inputs):
        path = f"identity.source_inputs[{index}]"
        if not reject_object_keys(source_input, {"type", "case_id", "source_id"}, path, errors):
            continue
        require_string(source_input.get("type"), f"{path}.type", errors, nonempty=True, normalized=True)
        for field in ("case_id", "source_id"):
            if field in source_input:
                require_string(source_input[field], f"{path}.{field}", errors, nonempty=True, normalized=True)


def validate_languages(languages, errors):
    if type(languages) is not list:
        errors.append("languages must be a list")
        return
    seen = {}
    allowed = {"language", "proficiency", "locale", "evidence", "confidence", "proficiency_explicit", "provenance"}
    for index, language in enumerate(languages):
        path = f"languages[{index}]"
        if not reject_object_keys(language, allowed, path, errors):
            continue
        require_string(language.get("language"), f"{path}.language", errors, nonempty=True)
        proficiency = language.get("proficiency")
        if proficiency not in LANGUAGE_PROFICIENCIES:
            errors.append(f"{path}.proficiency is not supported")
        require_string(language.get("locale", ""), f"{path}.locale", errors)
        validate_string_list(language.get("evidence", []), f"{path}.evidence", errors, unique=False)
        confidence = language.get("confidence", UNKNOWN)
        if confidence not in CONFIDENCE_LEVELS:
            errors.append(f"{path}.confidence is not supported")
        if "proficiency_explicit" in language and type(language["proficiency_explicit"]) is not bool:
            errors.append(f"{path}.proficiency_explicit must be boolean")
        if "provenance" in language and language["provenance"] not in PROFILE_SOURCES:
            errors.append(f"{path}.provenance is invalid")
        name = language.get("language")
        if type(name) is str:
            key = name.casefold()
            if key in seen:
                errors.append(
                    f"{path}.language duplicates languages[{seen[key]}].language; "
                    "language proficiency must be resolved explicitly"
                )
            seen[key] = index


def validate_location(location, errors):
    allowed = {
        "country", "region", "city", "timezone", "residence", "work_authorization",
        "eligible_countries", "remote_eligibility", "restrictions", "geographic_work_restrictions",
    }
    if not reject_object_keys(location, allowed, "location", errors):
        return
    for field in ("country", "region", "city", "timezone", "residence", "work_authorization"):
        require_string(location.get(field, ""), f"location.{field}", errors)
    for field in ("eligible_countries", "restrictions", "geographic_work_restrictions"):
        validate_string_list(location.get(field, []), f"location.{field}", errors)
    for field in ("country", "residence"):
        value = location.get(field)
        if value and not is_canonical_country(value):
            errors.append(f"location.{field} must be a canonical country name")
    for index, country in enumerate(location.get("eligible_countries", [])):
        if type(country) is str and not is_canonical_country(country):
            errors.append(
                f"location.eligible_countries[{index}] must be a canonical country name"
            )
    if location.get("remote_eligibility", UNKNOWN) not in REMOTE_ELIGIBILITY_STATUSES:
        errors.append("location.remote_eligibility is not supported")


def validate_education(education, errors):
    allowed = {"education_level", "degrees", "fields_or_domains", "institutions", "graduation_years", "completion_status"}
    if not reject_object_keys(education, allowed, "education", errors):
        return
    if education.get("education_level") not in EDUCATION_LEVELS:
        errors.append("education.education_level is not supported")
    for field in ("degrees", "fields_or_domains", "institutions"):
        validate_string_list(education.get(field, []), f"education.{field}", errors)
    years = education.get("graduation_years", [])
    if type(years) is not list:
        errors.append("education.graduation_years must be a list")
    else:
        for index, year in enumerate(years):
            if type(year) is not int or not 1900 <= year <= 2200:
                errors.append(f"education.graduation_years[{index}] must be an integer from 1900 to 2200")
    if education.get("completion_status", UNKNOWN) not in EDUCATION_COMPLETION_STATUSES:
        errors.append("education.completion_status is not supported")


def validate_credentials(credentials, errors):
    allowed = {"certifications", "licenses", "jurisdictions", "security_clearances", "credential_status"}
    if not reject_object_keys(credentials, allowed, "credentials", errors):
        return
    for field in ("certifications", "licenses", "jurisdictions", "security_clearances"):
        validate_string_list(credentials.get(field, []), f"credentials.{field}", errors)
    if credentials.get("credential_status") not in CREDENTIAL_STATUSES:
        errors.append("credentials.credential_status is not supported")


def validate_experience(experience, errors):
    allowed = {
        "total_years", "years_by_domain", "seniority", "recent_roles", "occupational_families",
        "job_titles", "professional_domains", "industries", "contribution_type", "specialties",
    }
    if not reject_object_keys(experience, allowed, "experience", errors):
        return
    years = experience.get("total_years")
    if years is not None and (type(years) is not int or not 0 <= years <= 80):
        errors.append("experience.total_years must be null or an integer from 0 to 80")
    by_domain = experience.get("years_by_domain", {})
    if type(by_domain) is not dict:
        errors.append("experience.years_by_domain must be an object")
    else:
        for domain, value in by_domain.items():
            require_string(domain, "experience.years_by_domain key", errors, nonempty=True)
            if type(value) not in {int, float} or value < 0:
                errors.append(f"experience.years_by_domain[{domain!r}] must be a non-negative number")
    if experience.get("seniority", UNKNOWN) not in SENIORITY_LEVELS:
        errors.append("experience.seniority is not supported")
    if experience.get("contribution_type", UNKNOWN) not in CONTRIBUTION_TYPES:
        errors.append("experience.contribution_type is not supported")
    for field in ("recent_roles", "occupational_families", "job_titles", "professional_domains", "industries", "specialties"):
        validate_string_list(experience.get(field, []), f"experience.{field}", errors)


def validate_skills(skills, errors):
    allowed = {
        "normalized", "free_text_labels", "entries", "technical", "software_tools",
        "writing_research", "administrative_support", "domain_specific",
    }
    if not reject_object_keys(skills, allowed, "skills", errors):
        return
    for field in ("normalized", "free_text_labels", "technical", "software_tools", "writing_research", "administrative_support", "domain_specific"):
        validate_string_list(skills.get(field, []), f"skills.{field}", errors)
    entries = skills.get("entries", [])
    if type(entries) is not list:
        errors.append("skills.entries must be a list")
        return
    for index, entry in enumerate(entries):
        path = f"skills.entries[{index}]"
        if not reject_object_keys(entry, {"skill", "evidence", "confidence", "provenance"}, path, errors):
            continue
        require_string(entry.get("skill"), f"{path}.skill", errors, nonempty=True)
        validate_string_list(entry.get("evidence", []), f"{path}.evidence", errors, unique=False)
        if entry.get("confidence", UNKNOWN) not in CONFIDENCE_LEVELS:
            errors.append(f"{path}.confidence is not supported")
        if "provenance" in entry and entry["provenance"] not in PROFILE_SOURCES:
            errors.append(f"{path}.provenance is invalid")


def validate_preferences(preferences, errors):
    allowed = {
        "remote", "flexible", "employment_types", "synchronous_preference", "phone_preference",
        "schedule", "availability", "rate_pay_preference", "target_opportunity_types",
        "preferred_task_types", "work_preferences",
    }
    if not reject_object_keys(preferences, allowed, "preferences", errors):
        return
    for field in ("remote", "flexible"):
        if type(preferences.get(field)) is not bool:
            errors.append(f"preferences.{field} must be boolean")
    for field in ("synchronous_preference", "phone_preference", "availability", "rate_pay_preference"):
        require_string(preferences.get(field, ""), f"preferences.{field}", errors)
    for field in ("employment_types", "schedule", "target_opportunity_types", "preferred_task_types", "work_preferences"):
        validate_string_list(preferences.get(field, []), f"preferences.{field}", errors)
    validate_enum_list(
        preferences.get("employment_types", []),
        EMPLOYMENT_TYPES,
        "preferences.employment_types",
        errors,
    )
    validate_enum_list(
        preferences.get("schedule", []),
        SCHEDULE_PREFERENCES,
        "preferences.schedule",
        errors,
    )
    validate_enum_list(
        preferences.get("work_preferences", []),
        EMPLOYMENT_TYPES | {"remote", "flexible"},
        "preferences.work_preferences",
        errors,
    )
    if (
        "synchronous_preference" in preferences
        and preferences.get("synchronous_preference") not in SYNCHRONOUS_PREFERENCES
    ):
        errors.append("preferences.synchronous_preference is not supported")
    if "phone_preference" in preferences and preferences.get("phone_preference") not in PHONE_PREFERENCES:
        errors.append("preferences.phone_preference is not supported")
    if "availability" in preferences and preferences.get("availability") not in AVAILABILITY_STATUSES:
        errors.append("preferences.availability is not supported")
    schedule = set(preferences.get("schedule") or [])
    synchronous = preferences.get("synchronous_preference")
    if synchronous == "synchronous" and "asynchronous" in schedule:
        errors.append("synchronous preference conflicts with asynchronous schedule")
    if synchronous == "asynchronous" and "synchronous" in schedule:
        errors.append("asynchronous preference conflicts with synchronous schedule")
    work_preferences = set(preferences.get("work_preferences") or [])
    if not preferences.get("remote") and "remote" in work_preferences:
        errors.append("remote work preference conflicts with preferences.remote=false")
    if not preferences.get("flexible") and "flexible" in work_preferences:
        errors.append("flexible work preference conflicts with preferences.flexible=false")


def validate_enum_list(value, allowed, path, errors):
    if type(value) is not list:
        return
    for index, item in enumerate(value):
        if type(item) is str and item not in allowed:
            errors.append(f"{path}[{index}] is not supported")


def validate_constraints(constraints, errors):
    allowed = {
        "hard_constraints", "soft_preferences", "avoid_keywords", "negative_constraints",
        "excluded_domains", "accessibility_constraints",
    }
    if not reject_object_keys(constraints, allowed, "constraints", errors):
        return
    for field in allowed:
        validate_string_list(constraints.get(field, []), f"constraints.{field}", errors)


def validate_derived_signals(derived, errors):
    allowed = {"signals", "derived_domains", "derived_target_work_types", "avoid_keywords"}
    if not reject_object_keys(derived, allowed, "derived_matcher_signals", errors):
        return
    for field in ("derived_domains", "derived_target_work_types", "avoid_keywords"):
        validate_string_list(derived.get(field, []), f"derived_matcher_signals.{field}", errors)
    signals = derived.get("signals")
    if type(signals) is not list:
        errors.append("derived_matcher_signals.signals must be a list")
        return
    for index, signal in enumerate(signals):
        path = f"derived_matcher_signals.signals[{index}]"
        if not reject_object_keys(signal, {"reason", "keywords", "points", "evidence", "confidence"}, path, errors):
            continue
        require_string(signal.get("reason"), f"{path}.reason", errors, nonempty=True)
        validate_string_list(signal.get("keywords"), f"{path}.keywords", errors)
        if type(signal.get("points")) is not int:
            errors.append(f"{path}.points must be an integer")
        validate_string_list(signal.get("evidence", []), f"{path}.evidence", errors, unique=False)
        if signal.get("confidence", UNKNOWN) not in CONFIDENCE_LEVELS:
            errors.append(f"{path}.confidence is not supported")


def validate_matcher_compatible(matcher_profile, errors):
    allowed = set(MATCHER_PROFILE_FIELDS) | {
        "language_proficiency", "recent_roles", "specialties", "total_years", "seniority",
        "certifications", "licenses", "credential_status", "phone_preference", "availability",
        "schedule", "negative_constraints",
    }
    if not reject_object_keys(matcher_profile, allowed, "matcher_compatible_profile", errors):
        return
    string_fields = {
        "profile_id", "display_name", "summary", "education_level", "notes", "location", "country",
        "residence", "city", "region", "seniority", "credential_status", "phone_preference", "availability",
    }
    list_fields = {
        "degrees_or_domains", "languages", "skills", "work_preferences", "constraints",
        "target_opportunity_types", "avoid_keywords", "recent_roles", "specialties", "certifications",
        "licenses", "schedule", "negative_constraints",
    }
    for field in string_fields.intersection(matcher_profile):
        require_string(matcher_profile[field], f"matcher_compatible_profile.{field}", errors)
    for field in list_fields.intersection(matcher_profile):
        validate_string_list(matcher_profile[field], f"matcher_compatible_profile.{field}", errors)
    years = matcher_profile.get("total_years")
    if years is not None and (type(years) is not int or not 0 <= years <= 80):
        errors.append("matcher_compatible_profile.total_years must be null or an integer from 0 to 80")
    proficiency = matcher_profile.get("language_proficiency")
    if proficiency is not None:
        if type(proficiency) is not dict:
            errors.append("matcher_compatible_profile.language_proficiency must be an object")
        else:
            for language, level in proficiency.items():
                require_string(language, "matcher_compatible_profile.language_proficiency key", errors, nonempty=True)
                if level not in LANGUAGE_PROFICIENCIES:
                    errors.append(f"matcher_compatible_profile.language_proficiency[{language!r}] is not supported")
    signals = matcher_profile.get("signals")
    if signals is not None:
        if type(signals) is not list:
            errors.append("matcher_compatible_profile.signals must be a list")
        else:
            for index, signal in enumerate(signals):
                path = f"matcher_compatible_profile.signals[{index}]"
                if type(signal) not in {list, tuple} or len(signal) != 3:
                    errors.append(f"{path} must contain reason, keywords, and points")
                    continue
                reason, keywords, points = signal
                require_string(reason, f"{path}[0]", errors, nonempty=True, normalized=True)
                validate_string_list(keywords, f"{path}[1]", errors)
                if type(points) is not int:
                    errors.append(f"{path}[2] must be an integer")


_PROVENANCE_ROOTS = {
    "identity",
    "languages",
    "location",
    "education",
    "credentials",
    "experience",
    "skills",
    "preferences",
    "constraints",
}
_PATH_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_ -]*")


def canonical_field_path_value(canonical_profile, path):
    """Resolve one material canonical leaf path or raise ValueError."""
    if type(path) is not str or not path or path != path.strip():
        raise ValueError("path must be a normalized non-empty string")
    position = 0
    tokens = []
    while position < len(path):
        match = _PATH_SEGMENT.match(path[position:])
        if not match:
            raise ValueError("path has a malformed field segment")
        tokens.append(match.group(0))
        position += len(match.group(0))
        while position < len(path) and path[position] == "[":
            closing = path.find("]", position + 1)
            if closing < 0:
                raise ValueError("path has an unterminated array index")
            index_text = path[position + 1 : closing]
            if not index_text.isdigit() or (len(index_text) > 1 and index_text[0] == "0"):
                raise ValueError("path has an invalid array index")
            tokens.append(int(index_text))
            position = closing + 1
        if position == len(path):
            break
        if path[position] != ".":
            raise ValueError("path has a malformed separator")
        position += 1
        if position == len(path):
            raise ValueError("path cannot end with a separator")

    if not tokens or tokens[0] not in _PROVENANCE_ROOTS:
        raise ValueError("path does not identify a material canonical field")
    value = canonical_profile
    for token in tokens:
        if type(token) is int:
            if type(value) is not list or token >= len(value):
                raise ValueError("path array index does not exist")
            value = value[token]
        else:
            if type(value) is not dict or token not in value:
                raise ValueError("path field does not exist")
            value = value[token]
    if type(value) in {dict, list}:
        raise ValueError("path must identify a leaf value")
    if value in (None, ""):
        raise ValueError("path identifies a value that is not present")
    return value


def validate_provenance(canonical_profile, errors):
    provenance = canonical_profile.get("provenance")
    allowed = {
        "extracted_from", "evidence_snippets", "original_text", "confidence", "missing_fields",
        "ambiguous_fields", "field_sources", "reviewed",
    }
    if not reject_object_keys(provenance, allowed, "provenance", errors):
        return
    require_string(provenance.get("extracted_from"), "provenance.extracted_from", errors, nonempty=True)
    validate_string_list(provenance.get("evidence_snippets", []), "provenance.evidence_snippets", errors, unique=False)
    if "original_text" in provenance:
        require_string(provenance["original_text"], "provenance.original_text", errors)
    if provenance.get("confidence") not in CONFIDENCE_LEVELS:
        errors.append("provenance.confidence is not supported")
    for field in ("missing_fields", "ambiguous_fields"):
        validate_string_list(provenance.get(field, []), f"provenance.{field}", errors)
    if "reviewed" in provenance and type(provenance["reviewed"]) is not bool:
        errors.append("provenance.reviewed must be boolean")
    field_sources = provenance.get("field_sources")
    if field_sources is None:
        errors.append("provenance.field_sources is required")
        return
    if type(field_sources) is not dict:
        errors.append("provenance.field_sources must be an object")
        return
    normalized_paths = set()
    for path, detail in field_sources.items():
        require_string(path, "provenance.field_sources key", errors, nonempty=True)
        detail_path = f"provenance.field_sources[{path!r}]"
        if type(path) is str:
            folded = path.casefold()
            if folded in normalized_paths:
                errors.append(f"{detail_path} duplicates another provenance path")
            normalized_paths.add(folded)
            try:
                canonical_field_path_value(canonical_profile, path)
            except ValueError as exc:
                errors.append(f"{detail_path} is invalid: {exc}")
        if not reject_object_keys(detail, {"source", "explicit"}, detail_path, errors):
            continue
        if detail.get("source") not in PROFILE_SOURCES:
            errors.append(f"{detail_path}.source is invalid")
        if type(detail.get("explicit")) is not bool:
            errors.append(f"{detail_path}.explicit must be boolean")
    expected_paths = set(
        field_sources_for_profile(
            canonical_profile,
            PROFILE_SOURCE_PARSED_TEXT,
            explicit=False,
        )
    )
    missing_paths = sorted(expected_paths - set(field_sources))
    for path in missing_paths:
        errors.append(f"provenance.field_sources is missing material field {path}")


def validate_profile_contradictions(profile, errors):
    if not all(type(profile.get(field)) is dict for field in ("education", "credentials", "experience", "constraints", "location")):
        return
    education = profile["education"]
    credentials = profile["credentials"]
    experience = profile["experience"]
    constraints = profile["constraints"]
    location = profile["location"]
    if credentials.get("credential_status") == ABSENT and any(
        credentials.get(field) for field in ("certifications", "licenses", "security_clearances")
    ):
        errors.append("credentials cannot be listed when credential_status is absent")
    if education.get("education_level") == "no_degree" and education.get("degrees"):
        errors.append("degrees cannot be listed when education_level is no_degree")
    excluded = {str(value).casefold() for value in constraints.get("excluded_domains", [])}
    positive_domains = {
        str(value).casefold()
        for value in list(education.get("fields_or_domains", []))
        + list(experience.get("professional_domains", []))
    }
    overlap = sorted(excluded & positive_domains)
    if overlap:
        errors.append("excluded domains conflict with confirmed professional domains: " + ", ".join(overlap))
    hard_constraints = {
        str(value).strip().casefold()
        for value in constraints.get("hard_constraints", [])
    }
    if (
        hard_constraints.intersection({"no degree", "no college degree", "no university degree"})
        and (
            education.get("degrees")
            or education.get("education_level")
            in {"associate", "bachelor", "master", "doctorate", "professional_degree"}
        )
    ):
        errors.append("completed degree evidence conflicts with an explicit no-degree constraint")
    listed_licenses = {
        str(value).strip().casefold()
        for value in credentials.get("licenses", [])
    }
    generic_no_license = bool(
        hard_constraints.intersection(
            {"no license", "no licenses", "no professional license", "no professional licenses"}
        )
    )
    medical_license_conflict = (
        "no medical license" in hard_constraints
        and any(
            token in license_name
            for license_name in listed_licenses
            for token in ("medical", "physician", "doctor", "clinical")
        )
    )
    legal_license_conflict = (
        "no law license" in hard_constraints
        and any(
            token in license_name
            for license_name in listed_licenses
            for token in ("law", "attorney", "bar")
        )
    )
    if listed_licenses and (generic_no_license or medical_license_conflict or legal_license_conflict):
        errors.append("listed professional licenses conflict with an explicit no-license constraint")
    if experience.get("total_years") == 0 and experience.get("seniority") in {"senior", "lead", "principal", "executive"}:
        errors.append("zero experience conflicts with confirmed senior professional experience")
    country = str(location.get("country") or "").casefold()
    residence = str(location.get("residence") or "").casefold()
    if country and residence and country != residence:
        errors.append("location.country conflicts with location.residence")


def canonical_profile_debug_summary(canonical_profile):
    validate_canonical_profile(canonical_profile)
    provenance = canonical_profile["provenance"]
    return {
        "profile_id": canonical_profile["identity"]["profile_id"],
        "schema_version": canonical_profile["schema_version"],
        "language_count": len(canonical_profile["languages"]),
        "skill_count": len(canonical_profile["skills"]["normalized"]),
        "target_opportunity_type_count": len(canonical_profile["preferences"]["target_opportunity_types"]),
        "signal_count": len(canonical_profile["derived_matcher_signals"]["signals"]),
        "has_location": any(
            canonical_profile["location"].get(field)
            for field in ("country", "region", "city", "residence")
        ),
        "has_credentials": bool(
            canonical_profile["credentials"]["certifications"]
            or canonical_profile["credentials"]["licenses"]
        ),
        "missing_fields": list(provenance.get("missing_fields") or []),
        "ambiguous_fields": list(provenance.get("ambiguous_fields") or []),
    }


def signal_to_canonical(signal):
    if isinstance(signal, dict):
        reason = signal.get("reason", "")
        keywords = signal.get("keywords", [])
        points = signal.get("points", 0)
    else:
        reason, keywords, points = signal
    return {
        "reason": str(reason),
        "keywords": [str(keyword) for keyword in (keywords or [])],
        "points": int(points),
        "evidence": [],
        "confidence": UNKNOWN,
    }


def signal_from_canonical(signal):
    return (
        signal["reason"],
        list(signal["keywords"]),
        int(signal["points"]),
    )


def string_value(profile, field):
    value = profile.get(field, "")
    if value is None:
        return ""
    return str(value).strip()


def list_value(profile, field):
    value = profile.get(field)
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item not in (None, "")]
    return [value]


def default_matcher_field_value(field):
    if field in {
        "degrees_or_domains",
        "languages",
        "skills",
        "work_preferences",
        "constraints",
        "target_opportunity_types",
        "signals",
        "avoid_keywords",
    }:
        return []
    if field == "education_level":
        return "not_specified"
    return ""


def normalized_set(values):
    return {str(value).strip().lower() for value in values}


def employment_types(work_preferences):
    terms = normalized_set(work_preferences)
    result = []
    for term in ("part-time", "full-time", "freelance", "contract", "entry-level"):
        if term in terms:
            result.append(term)
    return result


def preference_status(work_preferences, term):
    return "explicit" if term in normalized_set(work_preferences) else UNKNOWN


def missing_fields_for_profile(profile):
    missing = []
    if not any(string_value(profile, field) for field in ("location", "country", "residence", "city", "region")):
        missing.append("location")
    for field in ("certifications", "licenses", "seniority", "total_years"):
        if not profile.get(field):
            missing.append(field)
    return missing


def unique_strings(values):
    result = []
    seen = set()
    for value in values or []:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result

"""Canonical Profile Schema V1 and adapters.

This module defines a versioned profile contract that can later receive facts
from resumes, LinkedIn-style profiles, paragraphs, or sparse self-descriptions.
For now it only adapts the existing matcher profile dictionaries without
changing matcher behavior.
"""

from __future__ import annotations

from copy import deepcopy


SCHEMA_VERSION = "canonical_profile_v1"
UNKNOWN = "unknown"
ABSENT = "absent"

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

    canonical = {
        "schema_version": SCHEMA_VERSION,
        "identity": {
            "profile_id": string_value(profile, "profile_id"),
            "display_name": string_value(profile, "display_name"),
            "source_inputs": source_inputs,
        },
        "languages": languages,
        "location": {
            "country": string_value(profile, "country"),
            "region": string_value(profile, "region"),
            "city": string_value(profile, "city"),
            "timezone": string_value(profile, "timezone"),
            "residence": string_value(profile, "residence"),
            "work_authorization": UNKNOWN,
            "eligible_countries": [],
            "remote_eligibility": preference_status(work_preferences, "remote"),
            "restrictions": [],
        },
        "education": {
            "education_level": string_value(profile, "education_level") or "not_specified",
            "degrees": list_value(profile, "degrees"),
            "fields_or_domains": degrees_or_domains,
            "institutions": list_value(profile, "institutions"),
            "graduation_years": list_value(profile, "graduation_years"),
        },
        "credentials": {
            "certifications": list_value(profile, "certifications"),
            "licenses": list_value(profile, "licenses"),
            "jurisdictions": list_value(profile, "jurisdictions"),
            "credential_status": profile.get("credential_status") or UNKNOWN,
        },
        "experience": {
            "total_years": profile.get("total_years"),
            "years_by_domain": dict(profile.get("years_by_domain") or {}),
            "seniority": string_value(profile, "seniority") or UNKNOWN,
            "recent_roles": list_value(profile, "recent_roles"),
            "professional_domains": degrees_or_domains,
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
        },
        "preferences": {
            "remote": "remote" in normalized_set(work_preferences),
            "flexible": "flexible" in normalized_set(work_preferences),
            "employment_types": employment_types(work_preferences),
            "phone_preference": string_value(profile, "phone_preference") or UNKNOWN,
            "schedule": list_value(profile, "schedule"),
            "availability": string_value(profile, "availability") or UNKNOWN,
            "rate_pay_preference": string_value(profile, "rate_pay_preference"),
            "target_opportunity_types": target_types,
            "work_preferences": work_preferences,
        },
        "constraints": {
            "hard_constraints": constraints,
            "soft_preferences": [],
            "avoid_keywords": avoid_keywords,
            "negative_constraints": list_value(profile, "negative_constraints"),
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
    validate_canonical_profile(canonical)
    return canonical


def canonical_to_matcher_profile(canonical_profile):
    """Convert Canonical Profile Schema V1 back to the current matcher profile dict."""
    validate_canonical_profile(canonical_profile)
    matcher_profile = deepcopy(canonical_profile.get("matcher_compatible_profile") or {})
    identity = canonical_profile["identity"]
    matcher_profile["profile_id"] = identity["profile_id"]
    matcher_profile["display_name"] = identity["display_name"]
    matcher_profile["summary"] = string_value(matcher_profile, "summary")
    matcher_profile["education_level"] = string_value(matcher_profile, "education_level") or "not_specified"
    matcher_profile["degrees_or_domains"] = list_value(matcher_profile, "degrees_or_domains")
    matcher_profile["languages"] = list_value(matcher_profile, "languages")
    matcher_profile["skills"] = list_value(matcher_profile, "skills")
    matcher_profile["work_preferences"] = list_value(matcher_profile, "work_preferences")
    matcher_profile["constraints"] = list_value(matcher_profile, "constraints")
    matcher_profile["target_opportunity_types"] = list_value(matcher_profile, "target_opportunity_types")
    matcher_profile["notes"] = string_value(matcher_profile, "notes")
    matcher_profile["avoid_keywords"] = list_value(matcher_profile, "avoid_keywords")
    matcher_profile["signals"] = [
        signal_from_canonical(signal)
        for signal in canonical_profile["derived_matcher_signals"]["signals"]
    ]
    for field in ("location", "country", "residence", "city", "region"):
        matcher_profile[field] = string_value(matcher_profile, field)
    return matcher_profile


def validate_canonical_profile(canonical_profile):
    errors = canonical_profile_errors(canonical_profile)
    if errors:
        raise ValueError("; ".join(errors))
    return True


def canonical_profile_errors(canonical_profile):
    errors = []
    if not isinstance(canonical_profile, dict):
        return ["canonical profile must be an object"]
    if canonical_profile.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")

    identity = canonical_profile.get("identity")
    if not isinstance(identity, dict):
        errors.append("identity must be an object")
    else:
        for field in ("profile_id", "display_name"):
            if not isinstance(identity.get(field), str) or not identity[field].strip():
                errors.append(f"identity.{field} must be a non-empty string")
        if not isinstance(identity.get("source_inputs", []), list):
            errors.append("identity.source_inputs must be a list")

    for field in ("languages",):
        if not isinstance(canonical_profile.get(field), list):
            errors.append(f"{field} must be a list")
    for index, language in enumerate(canonical_profile.get("languages") or [], start=1):
        if not isinstance(language, dict) or not isinstance(language.get("language"), str) or not language["language"].strip():
            errors.append(f"languages[{index}] must have a language string")

    for field in (
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
    ):
        if not isinstance(canonical_profile.get(field), dict):
            errors.append(f"{field} must be an object")

    signals = (
        canonical_profile.get("derived_matcher_signals", {})
        if isinstance(canonical_profile.get("derived_matcher_signals"), dict)
        else {}
    ).get("signals", [])
    if not isinstance(signals, list):
        errors.append("derived_matcher_signals.signals must be a list")
    else:
        for index, signal in enumerate(signals, start=1):
            if not isinstance(signal, dict):
                errors.append(f"derived_matcher_signals.signals[{index}] must be an object")
                continue
            if not isinstance(signal.get("reason"), str) or not signal["reason"].strip():
                errors.append(f"derived_matcher_signals.signals[{index}].reason must be a non-empty string")
            if not isinstance(signal.get("keywords"), list):
                errors.append(f"derived_matcher_signals.signals[{index}].keywords must be a list")
            if not isinstance(signal.get("points"), int):
                errors.append(f"derived_matcher_signals.signals[{index}].points must be an integer")

    return errors


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

"""Profile normalizer interface and deterministic baseline implementations.

This module is infrastructure for future resume, LinkedIn-style, or free-text
profile extraction. It deliberately does not route production matching through
the normalizer yet.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import re
from typing import Protocol

from wahojobs.matching.languages import find_language_mentions, normalize_language_text
from wahojobs.profiles.canonical import SCHEMA_VERSION, UNKNOWN, validate_canonical_profile


EXTRACTION_QUALITY_CONTROL = "control"
EXTRACTION_QUALITY_BASELINE = "baseline"
EXTRACTION_QUALITY_LOW = "low"

CANONICAL_COMPARISON_FIELDS = (
    "languages",
    "location",
    "education",
    "credentials",
    "experience",
    "skills",
    "preferences",
    "constraints",
)

LANGUAGE_DISPLAY_NAMES = {
    "english": "English",
    "spanish": "Spanish",
    "portuguese": "Portuguese",
    "french": "French",
    "chinese": "Chinese",
    "kiswahili": "Kiswahili",
}


@dataclass
class NormalizationResult:
    """Result returned by profile normalizers."""

    canonical_profile: dict
    warnings: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    ambiguous_fields: list[str] = field(default_factory=list)
    extraction_quality: str = UNKNOWN
    metadata: dict = field(default_factory=dict)

    def validate(self) -> bool:
        validate_canonical_profile(self.canonical_profile)
        return True


class ProfileNormalizer(Protocol):
    """Contract for deterministic or reviewed canonical-profile extraction."""

    name: str

    def normalize(
        self,
        raw_input: str,
        input_style: str,
        metadata: dict | None = None,
    ) -> NormalizationResult:
        """Return a Canonical Profile Schema V1 result for the input."""


class FixtureExpectedProfileNormalizer:
    """Evaluation-control normalizer that returns suite expected profiles."""

    name = "fixture"

    def __init__(self, suite: dict):
        self.cases_by_id = {
            case["case_id"]: case
            for case in suite.get("cases", [])
        }

    def normalize(
        self,
        raw_input: str,
        input_style: str,
        metadata: dict | None = None,
    ) -> NormalizationResult:
        metadata = dict(metadata or {})
        case_id = metadata.get("case_id")
        if not case_id or case_id not in self.cases_by_id:
            raise ValueError("FixtureExpectedProfileNormalizer requires a known case_id.")
        case = self.cases_by_id[case_id]
        canonical = deepcopy(case["expected_canonical_profile"])
        warnings = []
        if raw_input != case.get("raw_input"):
            warnings.append("Provided raw_input differs from fixture raw_input.")
        if input_style != case.get("input_style"):
            warnings.append("Provided input_style differs from fixture input_style.")
        result = NormalizationResult(
            canonical_profile=canonical,
            warnings=warnings,
            missing_fields=list(canonical["provenance"].get("missing_fields") or []),
            ambiguous_fields=list(canonical["provenance"].get("ambiguous_fields") or []),
            extraction_quality=EXTRACTION_QUALITY_CONTROL,
            metadata={"case_id": case_id, "normalizer": self.name},
        )
        result.validate()
        return result


class BaselineHeuristicProfileNormalizer:
    """Small local baseline that extracts only obvious stated facts.

    This is intentionally conservative. It is useful for exercising the
    evaluation harness, not as a production resume parser.
    """

    name = "baseline"

    def normalize(
        self,
        raw_input: str,
        input_style: str,
        metadata: dict | None = None,
    ) -> NormalizationResult:
        metadata = dict(metadata or {})
        raw_input = str(raw_input or "")
        text = normalize_language_text(raw_input)
        case_id = metadata.get("case_id", "")
        archetype_id = metadata.get("archetype_id") or metadata.get("profile_id") or "normalized_profile"
        display_name = metadata.get("display_name") or display_name_from_profile_id(archetype_id)
        warnings = ["Baseline heuristic extracts only obvious stated facts."]

        languages = detect_profile_languages(raw_input)
        location = detect_location(text)
        education = detect_education(text)
        credentials = detect_credentials(text)
        experience = detect_experience(text)
        domains = detect_domains(text)
        skills = detect_skills(text, domains)
        preferences = detect_preferences(text, domains)
        constraints = detect_constraints(text)
        signals = signals_for_domains(domains, skills)
        missing_fields = missing_fields_for_baseline(languages, location, credentials, experience)
        ambiguous_fields = ambiguous_fields_for_baseline(text, input_style, languages)

        canonical = {
            "schema_version": SCHEMA_VERSION,
            "identity": {
                "profile_id": archetype_id,
                "display_name": display_name,
                "source_inputs": [
                    {
                        "type": input_style,
                        **({"case_id": case_id} if case_id else {}),
                    }
                ],
            },
            "languages": languages,
            "location": location,
            "education": education,
            "credentials": credentials,
            "experience": experience,
            "skills": skills_block(skills),
            "preferences": preferences,
            "constraints": constraints,
            "derived_matcher_signals": {
                "signals": signals,
                "derived_domains": domains,
                "derived_target_work_types": preferences["target_opportunity_types"],
                "avoid_keywords": constraints["avoid_keywords"],
            },
            "matcher_compatible_profile": matcher_profile_block(
                raw_input,
                archetype_id,
                display_name,
                education,
                languages,
                domains,
                skills,
                preferences,
                constraints,
                signals,
                location,
            ),
            "provenance": {
                "extracted_from": input_style,
                "evidence_snippets": [raw_input.strip()] if raw_input.strip() else [],
                "confidence": "low" if input_style == "messy_sparse_input" else "medium",
                "missing_fields": missing_fields,
                "ambiguous_fields": ambiguous_fields,
            },
        }
        result = NormalizationResult(
            canonical_profile=canonical,
            warnings=warnings,
            missing_fields=missing_fields,
            ambiguous_fields=ambiguous_fields,
            extraction_quality=EXTRACTION_QUALITY_BASELINE,
            metadata={
                "case_id": case_id,
                "normalizer": self.name,
                "archetype_id": archetype_id,
            },
        )
        result.validate()
        return result


def normalize_profile_input(
    raw_input: str,
    input_style: str,
    metadata: dict | None = None,
    normalizer: ProfileNormalizer | None = None,
) -> NormalizationResult:
    """Normalize raw input with the supplied normalizer or the baseline."""
    selected = normalizer or BaselineHeuristicProfileNormalizer()
    return selected.normalize(raw_input, input_style, metadata)


def compare_canonical_profiles(expected: dict, actual: dict) -> dict:
    """Compare expected and produced canonical profiles with deterministic fields."""
    validate_canonical_profile(expected)
    validate_canonical_profile(actual)
    field_results = []
    field_results.extend(compare_list_field("languages", expected_languages(expected), expected_languages(actual)))
    field_results.extend(
        compare_scalar_fields(
            "location",
            expected["location"],
            actual["location"],
            ("country", "region", "city", "residence", "remote_eligibility"),
        )
    )
    field_results.extend(
        compare_scalar_fields(
            "education",
            expected["education"],
            actual["education"],
            ("education_level",),
        )
    )
    field_results.extend(
        compare_list_field(
            "education.fields_or_domains",
            expected["education"].get("fields_or_domains") or [],
            actual["education"].get("fields_or_domains") or [],
        )
    )
    field_results.extend(
        compare_list_field(
            "education.degrees",
            expected["education"].get("degrees") or [],
            actual["education"].get("degrees") or [],
        )
    )
    for field_name in ("certifications", "licenses", "jurisdictions"):
        field_results.extend(
            compare_list_field(
                f"credentials.{field_name}",
                expected["credentials"].get(field_name) or [],
                actual["credentials"].get(field_name) or [],
            )
        )
    field_results.extend(
        compare_scalar_fields(
            "credentials",
            expected["credentials"],
            actual["credentials"],
            ("credential_status",),
        )
    )
    field_results.extend(
        compare_scalar_fields(
            "experience",
            expected["experience"],
            actual["experience"],
            ("total_years", "seniority"),
        )
    )
    for field_name in ("professional_domains", "specialties", "recent_roles"):
        field_results.extend(
            compare_list_field(
                f"experience.{field_name}",
                expected["experience"].get(field_name) or [],
                actual["experience"].get(field_name) or [],
            )
        )
    field_results.extend(
        compare_list_field(
            "skills.normalized",
            expected["skills"].get("normalized") or [],
            actual["skills"].get("normalized") or [],
        )
    )
    field_results.extend(
        compare_scalar_fields(
            "preferences",
            expected["preferences"],
            actual["preferences"],
            ("remote", "flexible", "phone_preference", "availability"),
        )
    )
    for field_name in ("employment_types", "target_opportunity_types", "work_preferences"):
        field_results.extend(
            compare_list_field(
                f"preferences.{field_name}",
                expected["preferences"].get(field_name) or [],
                actual["preferences"].get(field_name) or [],
            )
        )
    for field_name in ("hard_constraints", "soft_preferences", "avoid_keywords", "negative_constraints"):
        field_results.extend(
            compare_list_field(
                f"constraints.{field_name}",
                expected["constraints"].get(field_name) or [],
                actual["constraints"].get(field_name) or [],
            )
        )

    matched = sum(1 for item in field_results if item["match"])
    missing_critical = [
        item["field"]
        for item in field_results
        if not item["match"]
        and item.get("missing")
        and (
            item["field"].startswith("languages")
            or item["field"].startswith("credentials")
            or item["field"].startswith("location")
        )
    ]
    return {
        "exact_match": all(item["match"] for item in field_results),
        "matched_fields": matched,
        "total_fields": len(field_results),
        "field_match_rate": matched / len(field_results) if field_results else 1.0,
        "field_results": field_results,
        "missing_critical_fields": missing_critical,
    }


def compare_scalar_fields(prefix: str, expected: dict, actual: dict, fields: tuple[str, ...]) -> list[dict]:
    results = []
    for field_name in fields:
        expected_value = normalize_compare_scalar(expected.get(field_name))
        actual_value = normalize_compare_scalar(actual.get(field_name))
        results.append(
            {
                "field": f"{prefix}.{field_name}",
                "expected": expected.get(field_name),
                "actual": actual.get(field_name),
                "match": expected_value == actual_value,
                "missing": bool(expected_value) and not bool(actual_value),
                "extra": bool(actual_value) and not bool(expected_value),
            }
        )
    return results


def compare_list_field(field: str, expected: list, actual: list) -> list[dict]:
    expected_set = normalize_compare_set(expected)
    actual_set = normalize_compare_set(actual)
    missing = sorted(expected_set - actual_set)
    extra = sorted(actual_set - expected_set)
    return [
        {
            "field": field,
            "expected": sorted(expected_set),
            "actual": sorted(actual_set),
            "match": not missing and not extra,
            "missing": missing,
            "extra": extra,
        }
    ]


def normalize_compare_scalar(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    return normalize_language_text(value)


def normalize_compare_set(values) -> set[str]:
    return {
        normalize_language_text(value)
        for value in values
        if value not in (None, "")
    }


def expected_languages(canonical: dict) -> list[str]:
    return [entry.get("language", "") for entry in canonical.get("languages") or []]


def display_name_from_profile_id(profile_id: str) -> str:
    return profile_id.replace("_", " ").title() if profile_id else "Normalized Profile"


def detect_profile_languages(raw_input: str) -> list[dict]:
    mentions = find_language_mentions(raw_input)
    languages = []
    seen = set()
    for mention in mentions:
        language = mention["language"]
        if language in seen:
            continue
        seen.add(language)
        evidence = language_evidence(raw_input, mention)
        languages.append(
            {
                "language": LANGUAGE_DISPLAY_NAMES.get(language, language.title()),
                "proficiency": detect_language_proficiency(evidence, language),
                "locale": detect_language_locale(language, evidence),
                "evidence": [evidence.strip()] if evidence.strip() else [],
                "confidence": "high" if evidence.strip() else "medium",
            }
        )
    return languages


def language_evidence(raw_input: str, mention: dict) -> str:
    start = max(0, mention["start"] - 45)
    end = min(len(raw_input), mention["end"] + 60)
    return re.sub(r"\s+", " ", raw_input[start:end]).strip()


def detect_language_proficiency(evidence: str, language: str | None = None) -> str:
    text = normalize_language_text(evidence)
    if language:
        language = normalize_language_text(language)
        for term in ("native", "fluent", "advanced", "conversational"):
            if re.search(rf"\b{term}\s+{re.escape(language)}\b", text):
                return term
            if re.search(rf"\b{re.escape(language)}\s+{term}\b", text):
                return term
        if re.search(rf"\b{re.escape(language)}\s+reading\b", text):
            return "reading"
    for term in ("native", "fluent", "advanced", "conversational"):
        if term in text:
            return term
    if "reading" in text:
        return "reading"
    if "daily" in text:
        return "advanced"
    return UNKNOWN


def detect_language_locale(language: str, evidence: str) -> str:
    text = normalize_language_text(evidence)
    if language == "portuguese":
        if any(term in text for term in ("br", "brazilian", "pt br")):
            return "Brazil"
        if "portugal" in text or "pt pt" in text:
            return "Portugal"
    if language == "spanish" and "mexico" in text:
        return "Mexico"
    if language == "french" and "canada" in text:
        return "Canada"
    return ""


def detect_location(text: str) -> dict:
    country = ""
    region = ""
    city = ""
    if "sao paulo" in text:
        country = "Brazil"
        region = "Sao Paulo"
        city = "Sao Paulo"
    elif re.search(r"\b(in|location|located in|based in) brazil\b", text):
        country = "Brazil"
    return {
        "country": country,
        "region": region,
        "city": city,
        "timezone": "",
        "residence": country,
        "work_authorization": UNKNOWN,
        "eligible_countries": [],
        "remote_eligibility": UNKNOWN,
        "restrictions": [],
    }


def detect_education(text: str) -> dict:
    degrees = []
    fields = []
    level = "not_specified"
    if any(term in text for term in ("no college", "no degree", "high school")):
        level = "no_degree"
    if re.search(r"\bphd\b|doctorate", text):
        level = "doctorate"
        degrees.append("PhD")
    if re.search(r"\bjd\b", text):
        level = "professional_degree"
        degrees.append("JD")
    if re.search(r"\bba\b|bachelor", text):
        level = "bachelor"
        degrees.append("BA")
    fields = detect_domains(text)
    return {
        "education_level": level,
        "degrees": unique_list(degrees),
        "fields_or_domains": fields,
        "institutions": [],
        "graduation_years": [],
    }


def detect_credentials(text: str) -> dict:
    certifications = []
    licenses = []
    jurisdictions = []
    status = UNKNOWN
    if "cfa" in text:
        certifications.append("CFA Level II candidate")
        status = "in_progress"
    if "admitted in california" in text or "california bar" in text:
        licenses.append("attorney license")
        jurisdictions.append("California")
        status = "explicit"
    if has_medical_license_absence(text) or has_legal_license_absence(text) or has_biology_or_medical_credential_absence(text):
        status = "absent"
    return {
        "certifications": unique_list(certifications),
        "licenses": unique_list(licenses),
        "jurisdictions": unique_list(jurisdictions),
        "credential_status": status,
    }


def detect_experience(text: str) -> dict:
    years = None
    match = re.search(r"\b(\d{1,2})\s+years?\b", text)
    if match:
        years = int(match.group(1))
    domains = detect_domains(text)
    seniority = "senior" if "senior" in text or (years is not None and years >= 7) else UNKNOWN
    recent_roles = []
    for term in (
        "teacher",
        "tutor",
        "software engineer",
        "financial analyst",
        "research scientist",
        "lecturer",
        "historian",
        "lawyer",
        "attorney",
    ):
        if term in text:
            recent_roles.append(term)
    specialties = detect_specialties(text)
    return {
        "total_years": years,
        "years_by_domain": {},
        "seniority": seniority,
        "recent_roles": unique_list(recent_roles),
        "professional_domains": domains,
        "specialties": specialties,
    }


def detect_domains(text: str) -> list[str]:
    domains = []
    domain_terms = [
        ("software engineering", ("python", "typescript", "react", "software", "backend", "javascript", "coding", "code review")),
        ("legal", ("lawyer", "attorney", "legal", "contract", "ip", "jd", "law")),
        ("finance", ("finance", "financial", "accounting", "investment", "equity", "valuation", "cfa")),
        ("biology", ("biology", "microbiology", "microbiologist", "biologist", "antimicrobial")),
        ("microbiology", ("microbiology", "microbiologist", "antimicrobial")),
        ("medicine", ("medical", "medicine", "physician", "clinical", "dermatology")),
        ("history", ("history", "historian", "archival", "humanities")),
        ("education", ("teacher", "teaching", "tutor", "esl", "student", "grading")),
        ("language", ("translation", "translator", "localization", "mtpe", "subtitles", "language", "bilingual")),
        ("generalist", ("generalist", "annotation", "annotator", "online research", "web research", "content moderation", "data annotation", "review")),
    ]
    for domain, terms in domain_terms:
        if any(contains_positive_term(text, term) for term in terms):
            domains.append(domain)
    return unique_list(domains or ["generalist"])


def detect_specialties(text: str) -> list[str]:
    specialties = []
    for term in (
        "ip",
        "employment law",
        "contracts",
        "microbiology",
        "microbiologist",
        "antimicrobial resistance",
        "academic research",
        "academic writing",
        "backend",
        "test automation",
        "grammar correction",
        "source evaluation",
        "search quality",
        "audio validation",
        "subtitles",
    ):
        if contains_text_term(text, term):
            specialties.append("microbiology" if term == "microbiologist" else term)
    return unique_list(specialties)


def detect_skills(text: str, domains: list[str]) -> list[str]:
    skills = []
    skill_terms = [
        "python",
        "typescript",
        "react",
        "apis",
        "test automation",
        "writing",
        "review",
        "web research",
        "online research",
        "content moderation",
        "data annotation",
        "research",
        "academic writing",
        "translation",
        "localization",
        "mtpe",
        "audio validation",
        "grammar correction",
        "rubric grading",
        "source evaluation",
        "fact checking",
        "accounting review",
        "valuation models",
        "scientific writing",
    ]
    for term in skill_terms:
        if contains_text_term(text, term):
            skills.append(term)
    if "language" in domains and "bilingual communication" not in skills:
        skills.append("bilingual communication")
    if "generalist" in domains and "review" not in skills:
        skills.append("review")
    return unique_list(skills or ["review"])


def detect_preferences(text: str, domains: list[str]) -> dict:
    employment_types = []
    work_preferences = []
    target_types = []
    if "remote" in text:
        work_preferences.append("remote")
    if "flexible" in text:
        work_preferences.append("flexible")
    if "part time" in text or "part-time" in text:
        employment_types.append("part-time")
        work_preferences.append("part-time")
    if "freelance" in text:
        employment_types.append("freelance")
        work_preferences.append("freelance")
    if "contract" in text:
        employment_types.append("contract")
        work_preferences.append("contract")
    if "entry level" in text or "entry-level" in text or "beginner" in text:
        employment_types.append("entry-level")
        work_preferences.append("entry-level")
    if "data annotation" in text or "annotation" in text:
        target_types.append("data annotation")
    if "search" in text:
        target_types.append("search evaluation")
    if "coding" in text or "code" in text or "python" in text:
        target_types.append("AI coding evaluation")
    if "legal" in text or "law" in domains:
        target_types.append("legal AI training")
    if "finance" in domains:
        target_types.append("finance AI training")
    if "biology" in domains or "medicine" in domains:
        target_types.append("science AI training")
    if "language" in domains:
        target_types.append("language review")
    if not target_types:
        target_types.append("AI training")
    return {
        "remote": "remote" in work_preferences,
        "flexible": "flexible" in work_preferences,
        "employment_types": unique_list(employment_types),
        "phone_preference": "non-phone preferred" if "no phone" in text or "not calls" in text else UNKNOWN,
        "schedule": [],
        "availability": UNKNOWN,
        "rate_pay_preference": "",
        "target_opportunity_types": unique_list(target_types),
        "work_preferences": unique_list(work_preferences),
    }


def detect_constraints(text: str) -> dict:
    hard = []
    soft = []
    avoid = []
    if "no college" in text or "no degree" in text:
        hard.append("no college degree")
    if has_medical_license_absence(text):
        hard.append("no medical license")
        avoid.append("licensed physician")
    if has_legal_license_absence(text):
        hard.append("no law license")
        avoid.append("attorney license")
    if has_biology_or_medical_credential_absence(text):
        hard.append("no biology or medical credentials")
        avoid.extend(["biology credentials", "medical credentials"])
    if "no phone" in text or "not calls" in text:
        soft.append("no phone calls preferred")
        avoid.append("phone calls")
    if "not coding" in text:
        soft.append("not coding")
        avoid.append("coding")
    return {
        "hard_constraints": unique_list(hard),
        "soft_preferences": unique_list(soft),
        "avoid_keywords": unique_list(avoid),
        "negative_constraints": [],
    }


def signals_for_domains(domains: list[str], skills: list[str]) -> list[dict]:
    signals = []
    signal_rules = [
        ("Generalist AI-work signal", ["generalist", "ai trainer", "ai training"], 9, {"generalist"}),
        ("Data annotation signal", ["annotation", "annotator", "data validation"], 7, {"generalist"}),
        ("Language/translation signal", ["language", "translation", "translator", "localization", "bilingual"], 10, {"language"}),
        ("Software/coding signal", ["python", "coding", "software", "developer", "code"], 14, {"software engineering"}),
        ("Legal domain signal", ["legal", "law", "lawyer", "attorney", "contract"], 11, {"legal"}),
        ("Finance domain signal", ["finance", "accounting", "investment", "equity"], 10, {"finance"}),
        ("Science/medical signal", ["biology", "medical", "medicine", "science"], 10, {"biology", "medicine"}),
        ("Microbiology/research writing signal", ["microbiology", "research", "academic writing", "scientific writing"], 8, {"microbiology"}),
        ("Teaching/writing/review signal", ["teacher", "education", "writing", "review"], 9, {"education", "history"}),
    ]
    domain_set = set(domains)
    for reason, keywords, points, applies_to in signal_rules:
        if domain_set & applies_to:
            signals.append({"reason": reason, "keywords": keywords, "points": points})
    if not signals:
        signals.append({"reason": "General profile keyword match", "keywords": skills or ["review"], "points": 6})
    return signals


def skills_block(skills: list[str]) -> dict:
    return {
        "normalized": skills,
        "free_text_labels": skills,
        "entries": [
            {"skill": skill, "evidence": [], "confidence": "medium"}
            for skill in skills
        ],
    }


def matcher_profile_block(
    raw_input: str,
    profile_id: str,
    display_name: str,
    education: dict,
    languages: list[dict],
    domains: list[str],
    skills: list[str],
    preferences: dict,
    constraints: dict,
    signals: list[dict],
    location: dict,
) -> dict:
    return {
        "profile_id": profile_id,
        "display_name": display_name,
        "summary": raw_input,
        "education_level": education["education_level"],
        "degrees_or_domains": domains,
        "languages": [entry["language"] for entry in languages],
        "skills": skills,
        "work_preferences": preferences["work_preferences"],
        "constraints": constraints["hard_constraints"] + constraints["soft_preferences"],
        "target_opportunity_types": preferences["target_opportunity_types"],
        "notes": "",
        "signals": [
            (signal["reason"], signal["keywords"], signal["points"])
            for signal in signals
        ],
        "avoid_keywords": constraints["avoid_keywords"],
        "location": location["country"],
        "country": location["country"],
        "residence": location["residence"],
        "city": location["city"],
        "region": location["region"],
    }


def missing_fields_for_baseline(languages: list[dict], location: dict, credentials: dict, experience: dict) -> list[str]:
    missing = []
    if not languages:
        missing.append("languages")
    if not any(location.get(field) for field in ("country", "region", "city", "residence")):
        missing.append("location")
    if not credentials.get("certifications"):
        missing.append("certifications")
    if not credentials.get("licenses"):
        missing.append("licenses")
    if experience.get("total_years") is None:
        missing.append("total_years")
    return missing


def ambiguous_fields_for_baseline(text: str, input_style: str, languages: list[dict]) -> list[str]:
    ambiguous = []
    if input_style == "messy_sparse_input":
        ambiguous.append("messy_input")
    if languages and any(item["proficiency"] == UNKNOWN for item in languages):
        ambiguous.append("language proficiency")
    if "maybe" in text or "?" in text:
        ambiguous.append("user intent")
    if "remote" not in text:
        ambiguous.append("remote preference")
    return unique_list(ambiguous)


def unique_list(values: list) -> list:
    result = []
    seen = set()
    for value in values:
        if value in (None, ""):
            continue
        key = str(value).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def contains_text_term(text: str, term: str) -> bool:
    normalized_term = normalize_language_text(term)
    if not normalized_term:
        return False
    pattern = r"(?<![a-z0-9])" + r"\s+".join(
        re.escape(part) for part in normalized_term.split()
    ) + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


def contains_positive_term(text: str, term: str) -> bool:
    normalized_term = normalize_language_text(term)
    if not normalized_term:
        return False
    pattern = r"(?<![a-z0-9])" + r"\s+".join(
        re.escape(part) for part in normalized_term.split()
    ) + r"(?![a-z0-9])"
    for match in re.finditer(pattern, text):
        if not term_is_negated(text, match.start(), match.end()):
            return True
    return False


def term_is_negated(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 48):start]
    after = text[end:end + 36]
    if re.search(r"\b(no|not|without|lack|lacking)\b[^.]{0,40}\b(credentials?|license|licensed|degree)\b", before):
        return True
    if re.search(r"\b(do not|don t|dont|does not|doesn t|doesnt|no)\s+(have|hold|possess)\b", before):
        return True
    if re.search(r"\bnot\s+(a\s+)?licensed\b", before):
        return True
    if re.search(r"\bcredentials?\b", after) and re.search(r"\b(no|not|don t|dont|do not|without)\b", before):
        return True
    return False


def has_medical_license_absence(text: str) -> bool:
    return bool(
        re.search(r"\bnot\s+(a\s+)?licensed\s+physician\b", text)
        or re.search(r"\bno\s+(medical|clinical)\s+license\b", text)
        or re.search(r"\bmedical\s+license\s+(no|none|absent)\b", text)
    )


def has_legal_license_absence(text: str) -> bool:
    return bool(
        re.search(r"\bno\s+(law|legal|attorney)\s+license\b", text)
        or re.search(r"\bnot\s+(a\s+)?licensed\s+(attorney|lawyer)\b", text)
    )


def has_biology_or_medical_credential_absence(text: str) -> bool:
    return bool(
        re.search(r"\b(do not|don t|dont|no|without)\s+(have\s+)?(biology|medical)[^.,;]{0,30}credentials?\b", text)
        or re.search(r"\b(do not|don t|dont|no|without)\s+(have\s+)?biology\s+or\s+medical\s+credentials?\b", text)
        or re.search(r"\bno\s+(biology|medical)\s+credentials?\b", text)
    )

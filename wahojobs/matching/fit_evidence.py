"""Preview-only affirmative fit evidence for personalized recommendations.

This module deliberately does not score or rank opportunities. It evaluates
whether the profile contains affirmative evidence for the defining requirements
already visible in an opportunity title and the preview guardrail metadata.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass

from wahojobs.profiles.normalizer import term_is_negated


SUPPORTED = "supported"
UNCERTAIN = "uncertain"
CONFLICTING = "conflicting"


@dataclass(frozen=True)
class FitEvidence:
    requirement: str
    profile_evidence: str
    source: str


@dataclass(frozen=True)
class RequirementGroup:
    key: str
    label: str
    mode: str
    concepts: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class AffirmativeFitAssessment:
    status: str
    supported_evidence: tuple[FitEvidence, ...]
    required_groups: tuple[RequirementGroup, ...]
    satisfied_groups: tuple[str, ...]
    missing_requirements: tuple[str, ...]
    conflicting_requirements: tuple[str, ...]
    unmodeled_requirements: tuple[str, ...]
    location_and_locale_evidence: tuple[str, ...]
    adjacencies_used: tuple[str, ...]
    why_fit_statements: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FitConcept:
    key: str
    label: str
    aliases: tuple[str, ...]
    profile_aliases: tuple[str, ...]


@dataclass(frozen=True)
class ProfileFitEvidence:
    concepts: frozenset[str]
    evidence_by_concept: tuple[tuple[str, str], ...]
    adjacencies: tuple[str, ...]

    def evidence_map(self) -> dict[str, str]:
        return dict(self.evidence_by_concept)


ROLE_CONCEPTS = (
    FitConcept("frontend_development", "Frontend Development", ("frontend", "front end", "front-end", "ui developer"), ("frontend", "front end", "front-end")),
    FitConcept("backend_development", "Backend Development", ("backend", "back end", "back-end"), ("backend", "back end", "back-end")),
    FitConcept("infrastructure_engineering", "Infrastructure Engineering", ("infrastructure", "platform engineer"), ("infrastructure", "platform engineering")),
    FitConcept("full_stack_development", "Full Stack Development", ("full stack", "full-stack"), ("full stack", "full-stack")),
    FitConcept("machine_learning_engineering", "Machine Learning Engineering", ("ai/ml engineer", "machine learning engineer", "ml engineer", "ai engineer"), ("machine learning engineering", "ml engineering", "ai engineering")),
    FitConcept("mechanical_engineering", "Mechanical Engineering", ("mechanical engineer", "mechanical engineering"), ("mechanical engineer", "mechanical engineering")),
    FitConcept("mobile_development", "Mobile App Development", ("mobile app developer", "mobile developer", "ios developer", "android developer"), ("mobile app", "mobile development", "ios", "android")),
    FitConcept("site_reliability", "Site Reliability Engineering", ("site reliability", "sre", "devops"), ("site reliability", "sre", "devops", "production operations")),
    FitConcept("senior_level", "Senior-level experience", ("senior", "senior-level", "sr", "sr.", "principal"), ("senior", "senior-level", "principal")),
    FitConcept("biophysics", "Biophysics", ("biophysics", "biophysical"), ("biophysics", "biophysical")),
    FitConcept("biomedical", "Biomedical Science", ("biomedical",), ("biomedical",)),
    FitConcept("pharma", "Pharmaceutical Work", ("pharma", "pharmaceutical"), ("pharma", "pharmaceutical")),
    FitConcept("social_work", "Social Work", ("social worker", "social workers", "social work"), ("social worker", "social work")),
    FitConcept("data_science", "Data Science", ("data scientist", "data science"), ("data scientist", "data science")),
    FitConcept("physical_science", "Physical Science", ("physical scientist", "physical science"), ("physical scientist", "physical science")),
    FitConcept("writing", "Writing", ("writing specialist", "writing generalist", "writer"), ("writing", "writer", "copywriting", "editing")),
    FitConcept("audio_editing", "Audio Editing", ("audio editor", "audio editing", "post-production", "post production"), ("audio editor", "audio editing", "post-production", "post production")),
    FitConcept("audio_specialist", "Professional Audio Work", ("audio specialist", "audio engineer", "audio engineering"), ("audio specialist", "audio engineer", "audio engineering")),
    FitConcept("voice_acting", "Voice Acting", ("voice actor", "voice acting"), ("voice actor", "voice acting")),
    FitConcept("voice_coaching", "Voice Coaching", ("voice coach", "voice coaching"), ("voice coach", "voice coaching")),
    FitConcept("accent_dialect_work", "Accent or Dialect Expertise", ("accents/dialects", "accents and dialects", "accent and dialect"), ("accent expertise", "dialect expertise", "accent and dialect")),
    FitConcept("business_management", "Business Management", ("business and management", "business management", "management specialist"), ("business management", "management specialist", "manager")),
    FitConcept("business_ownership", "Business Ownership", ("business owner", "business owners", "entrepreneur"), ("business owner", "entrepreneur", "founded", "founder")),
    FitConcept("research", "Research", ("research scientist", "research specialist", "research quality"), ("research", "academic research", "research scientist")),
    FitConcept("stem_phd", "STEM PhD", ("stem phd", "stem ph.d", "stem doctorate"), ("stem phd", "stem doctorate")),
)

CONCEPT_BY_KEY = {concept.key: concept for concept in ROLE_CONCEPTS}

CONFLICTING_CAP_REASONS = {
    "personalized_eligibility_failed",
    "professional_domain_hard_gate",
    "explicit_credential_incompatibility",
    "unsupported_title_language_or_dialect",
    "incompatible_location",
}

GENERIC_AI_ROLE_PATTERNS = (
    r"\bai\s+(?:content\s+)?evaluat(?:ion|or)\b",
    r"\bai\s+data\s+reviewer\b",
    r"\bai\s+trainer(?:\s+and\s+evaluator)?\b",
    r"\bdata\s+annotation\b",
    r"\bimage\s+annotator\b",
    r"\bcrowd\s+workers?\b",
    r"\bgeneralist\s+ai\s+train",
)

LANGUAGE_DATA_PATTERNS = (
    r"\blanguage\s+data\s+contributor\b",
    r"\blanguage\s+expert\b",
    r"\blanguage\s+specialist\b",
)

SOFTWARE_ROLE_PATTERNS = (
    r"\bsoftware\s+engineer\b",
    r"\bsoftware\s+developer\b",
    r"\bcoding\s+(?:expert|specialist|evaluator)\b",
    r"\bengineer\s*:\s*all\s+domains\b",
)

UNMODELED_DEFINING_PATTERNS = (
    (r"\bpharmacokinetics\b", "Pharmacokinetics"),
    (r"\bsystems biology\b", "Systems Biology"),
    (r"\bnursing\b|\bnurse\b", "Nursing"),
    (r"\bdermatolog", "Dermatology"),
    (r"\bspecial education\b", "Special Education"),
    (r"\bhealthcare\b", "Healthcare"),
)


def build_profile_fit_evidence(profile: dict) -> ProfileFitEvidence:
    concepts, evidence, adjacencies = _profile_concept_evidence(profile)
    return ProfileFitEvidence(
        concepts=frozenset(concepts),
        evidence_by_concept=tuple(sorted(evidence.items())),
        adjacencies=tuple(adjacencies),
    )


def assess_affirmative_fit(
    profile: dict,
    row: dict,
    match: dict,
    profile_fit_evidence: ProfileFitEvidence | None = None,
) -> AffirmativeFitAssessment:
    """Assess affirmative fit without mutating raw matcher fields."""
    title = str(match.get("display_title") or row.get("title") or "")
    normalized_title = normalize_text(title)
    requirements: list[RequirementGroup] = []
    evidence: list[FitEvidence] = []
    satisfied: list[str] = []
    missing: list[str] = []
    conflicts: list[str] = []
    unmodeled: list[str] = []
    location_locale: list[str] = []
    adjacencies: list[str] = []

    cap_reasons = set(match.get("actionability_cap_reasons") or [])
    for reason in sorted(cap_reasons.intersection(CONFLICTING_CAP_REASONS)):
        conflicts.append(_cap_reason_label(reason))

    language_result = _evaluate_languages(match)
    requirements.extend(language_result["requirements"])
    evidence.extend(language_result["evidence"])
    satisfied.extend(language_result["satisfied"])
    missing.extend(language_result["missing"])
    conflicts.extend(language_result["conflicts"])
    if match.get("language_requirement_mode") == "ambiguous" and match.get("detected_languages"):
        unmodeled.append("Ambiguous multi-language requirement")

    specialization_result = _evaluate_existing_specializations(match, normalized_title)
    requirements.extend(specialization_result["requirements"])
    evidence.extend(specialization_result["evidence"])
    satisfied.extend(specialization_result["satisfied"])
    missing.extend(specialization_result["missing"])
    unmodeled.extend(specialization_result["unmodeled"])

    profile_fit_evidence = profile_fit_evidence or build_profile_fit_evidence(profile)
    profile_concepts = set(profile_fit_evidence.concepts)
    profile_evidence = profile_fit_evidence.evidence_map()
    profile_adjacencies = list(profile_fit_evidence.adjacencies)
    role_result = _evaluate_role_concepts(normalized_title, profile_concepts, profile_evidence)
    requirements.extend(role_result["requirements"])
    evidence.extend(role_result["evidence"])
    satisfied.extend(role_result["satisfied"])
    missing.extend(role_result["missing"])
    unmodeled.extend(role_result["unmodeled"])
    adjacencies.extend(
        adjacency for adjacency in profile_adjacencies if adjacency in role_result["used_adjacencies"]
    )

    location_result = _evaluate_location(profile, normalized_title)
    if location_result["requirement"]:
        requirements.append(location_result["requirement"])
    evidence.extend(location_result["evidence"])
    satisfied.extend(location_result["satisfied"])
    missing.extend(location_result["missing"])
    conflicts.extend(location_result["conflicts"])
    location_locale.extend(location_result["details"])
    if match.get("location_eligibility_status") == "incompatible":
        structured_reason = (
            match.get("location_eligibility_reason")
            or "Stored opportunity location is incompatible with the profile location."
        )
        conflicts.append(structured_reason)
        location_locale.append(structured_reason)

    credential_result = _evaluate_title_credentials(profile, normalized_title)
    requirements.extend(credential_result["requirements"])
    evidence.extend(credential_result["evidence"])
    satisfied.extend(credential_result["satisfied"])
    missing.extend(credential_result["missing"])
    conflicts.extend(credential_result["conflicts"])

    if "unconfirmed_language_locale" in cap_reasons:
        missing.append("Confirmed language locale or accent")
        location_locale.append("Language locale is not confirmed by the profile")
    if "unconfirmed_location_restriction" in cap_reasons and not location_result["requirement"]:
        missing.append("Confirmed location eligibility")
        location_locale.append("Location eligibility remains unconfirmed")
    if "location_actionability_cap" in cap_reasons:
        missing.append("Actionable location information")
    if match.get("preview_credential_requirement"):
        if "explicit_credential_incompatibility" in cap_reasons:
            conflicts.append(match["preview_credential_requirement"])
        else:
            missing.append(match["preview_credential_requirement"])

    generic_result = _evaluate_generic_role(profile, normalized_title, match, role_result)
    evidence.extend(generic_result["evidence"])
    satisfied.extend(generic_result["satisfied"])
    unmodeled.extend(generic_result["unmodeled"])

    if "specialized_annotation_mismatch" in cap_reasons:
        missing.append("Relevant specialized annotation or survey expertise")
    if "science_subdomain_mismatch" in cap_reasons:
        missing.append("Required science subdomain")
    if "absent_science_credentials" in cap_reasons:
        conflicts.append("Profile explicitly lacks the required science or medical credentials")
    if "medical_credential_requirement" in cap_reasons:
        missing.append("Required medical or professional credential")

    requirements = _unique_requirements(requirements)
    evidence = _unique_evidence(evidence)
    satisfied = unique(satisfied)
    missing = unique(missing)
    conflicts = unique(conflicts)
    unmodeled = unique(unmodeled)
    location_locale = unique(location_locale)
    adjacencies = unique(adjacencies)

    meaningful_evidence = [item for item in evidence if item.source not in {"location", "locale"}]
    uncertain_cap = bool(cap_reasons - CONFLICTING_CAP_REASONS)
    if conflicts:
        status = CONFLICTING
    elif missing or unmodeled or uncertain_cap or not meaningful_evidence:
        status = UNCERTAIN
    else:
        status = SUPPORTED

    why = _why_fit_statements(title, evidence, adjacencies) if status == SUPPORTED else []
    if status == SUPPORTED and not why:
        status = UNCERTAIN
        unmodeled.append("No grounded user-facing fit explanation")

    return AffirmativeFitAssessment(
        status=status,
        supported_evidence=tuple(evidence),
        required_groups=tuple(requirements),
        satisfied_groups=tuple(satisfied),
        missing_requirements=tuple(missing),
        conflicting_requirements=tuple(conflicts),
        unmodeled_requirements=tuple(unique(unmodeled)),
        location_and_locale_evidence=tuple(location_locale),
        adjacencies_used=tuple(adjacencies),
        why_fit_statements=tuple(why),
    )


def _evaluate_languages(match: dict) -> dict:
    detected = unique([normalize_text(value) for value in match.get("detected_languages") or []])
    matched = set(normalize_text(value) for value in match.get("matched_languages") or [])
    unsupported = set(normalize_text(value) for value in match.get("unsupported_languages") or [])
    mode = match.get("language_requirement_mode") or ("single" if len(detected) == 1 else "ambiguous")
    requirements = []
    evidence = []
    satisfied = []
    missing = []
    conflicts = []
    if not detected:
        return locals_subset(requirements, evidence, satisfied, missing, conflicts)
    if mode == "any_supported":
        label = " or ".join(value.title() for value in detected)
        requirements.append(RequirementGroup("language:any", label, "any_of", tuple(detected), "language"))
        present = sorted(matched.intersection(detected))
        if present:
            satisfied.append(label)
            evidence.append(FitEvidence(label, present[0].title(), "language"))
        else:
            conflicts.append(label)
    else:
        for language in detected:
            label = language.title()
            requirements.append(RequirementGroup(f"language:{language}", label, "all_of", (language,), "language"))
            if language in matched:
                satisfied.append(label)
                evidence.append(FitEvidence(label, label, "language"))
            elif language in unsupported:
                conflicts.append(label)
            else:
                missing.append(label)
    return locals_subset(requirements, evidence, satisfied, missing, conflicts)


def _evaluate_existing_specializations(match: dict, title: str) -> dict:
    requirements = []
    evidence = []
    satisfied = []
    missing = []
    unmodeled = []
    programming_concepts = {
        "python", "java", "kotlin", "c", "c_sharp", "c_plus_plus", "javascript",
        "typescript", "react", "rust", "go", "swift", "ruby", "php", "scala", "r", "matlab",
    }
    for group in match.get("specialization_requirements") or []:
        concepts = tuple(group.get("concepts") or [])
        ambiguous_slash = (
            group.get("mode") == "any_of"
            and "/" in title
            and not set(concepts).issubset(programming_concepts)
        )
        requirement = RequirementGroup(
            "specialization:" + "+".join(concepts),
            group.get("label") or "Specialization",
            "ambiguous" if ambiguous_slash else (group.get("mode") or "all_of"),
            concepts,
            "specialization",
        )
        requirements.append(requirement)
        if ambiguous_slash:
            unmodeled.append(f"Ambiguous slash requirement: {requirement.label}")
    for group in match.get("supported_specialization_groups") or []:
        label = group.get("label") or "Specialization"
        concepts = set(group.get("concepts") or [])
        if group.get("mode") == "any_of" and "/" in title and not concepts.issubset(programming_concepts):
            continue
        satisfied.append(label)
        matched = group.get("matched_concepts") or []
        evidence.append(FitEvidence(label, ", ".join(_display_concept(value) for value in matched) or label, "structured_profile"))
    for group in match.get("missing_specialization_groups") or []:
        missing.append(group.get("label") or "Specialization")
    result = locals_subset(requirements, evidence, satisfied, missing)
    result["unmodeled"] = unmodeled
    return result


def _profile_concept_evidence(profile: dict) -> tuple[set[str], dict[str, str], list[str]]:
    structured_values = [
        *(profile.get("degrees_or_domains") or []),
        *(profile.get("skills") or []),
        *(profile.get("target_opportunity_types") or []),
    ]
    structured_text = normalize_text(" ".join(str(value) for value in structured_values))
    raw_text = normalize_text(" ".join(str(profile.get(field) or "") for field in ("summary", "notes")))
    concepts = set()
    evidence = {}
    for concept in ROLE_CONCEPTS:
        structured_alias = next((alias for alias in concept.profile_aliases if contains_alias(structured_text, alias)), None)
        raw_alias = next((alias for alias in concept.profile_aliases if contains_positive_alias(raw_text, alias)), None)
        alias = structured_alias or raw_alias
        if alias:
            concepts.add(concept.key)
            evidence[concept.key] = concept.label if structured_alias else alias

    skills = {normalize_text(value) for value in profile.get("skills") or []}
    domains = {normalize_text(value) for value in profile.get("degrees_or_domains") or []}
    positive_raw = raw_text
    adjacencies = []
    if "react" in skills and "typescript" in skills:
        concepts.add("frontend_development")
        evidence["frontend_development"] = "React and TypeScript"
        adjacencies.append("React + TypeScript -> Frontend Development")
    if contains_positive_alias(positive_raw, "api") and (
        contains_positive_alias(positive_raw, "data platform") or "software engineering" in domains
    ):
        concepts.add("backend_development")
        evidence["backend_development"] = "API and data-platform experience"
        adjacencies.append("API/data-platform experience -> Backend Development")
    if contains_positive_alias(positive_raw, "data platform") and "software engineering" in domains:
        concepts.add("infrastructure_engineering")
        evidence["infrastructure_engineering"] = "data-platform experience"
        adjacencies.append("Data-platform experience -> Infrastructure Engineering")
    if "frontend_development" in concepts and "backend_development" in concepts:
        concepts.add("full_stack_development")
        evidence["full_stack_development"] = "React, TypeScript, and API experience"
        adjacencies.append("Frontend + backend evidence -> Full Stack Development")
    education = normalize_text(profile.get("education_level"))
    doctorate = education == "doctorate" or contains_positive_alias(positive_raw, "phd")
    if doctorate and domains.intersection({"biology", "microbiology", "software engineering", "physics", "chemistry"}):
        concepts.add("stem_phd")
        evidence["stem_phd"] = "STEM PhD"
    if domains.intersection({"biology", "microbiology"}) and contains_positive_alias(positive_raw, "research"):
        concepts.add("research")
        evidence["research"] = (
            "Biology PhD and research background"
            if doctorate
            else "Biology research background"
        )
        adjacencies.append("Biology + research evidence -> Biology Research")
    if normalize_text(profile.get("seniority")) in {"senior", "lead", "principal"} or contains_positive_alias(positive_raw, "senior"):
        concepts.add("senior_level")
        evidence["senior_level"] = "senior-level experience"
    return concepts, evidence, adjacencies


def _evaluate_role_concepts(title: str, concepts: set[str], profile_evidence: dict[str, str]) -> dict:
    mentions = _role_mentions(title)
    requirements = []
    evidence = []
    satisfied = []
    missing = []
    unmodeled = []
    used_adjacencies = []
    index = 0
    while index < len(mentions):
        current = mentions[index]
        slash_group = [current]
        while index + 1 < len(mentions):
            separator = title[current[1]:mentions[index + 1][0]]
            if not re.fullmatch(r"\s*/\s*", separator):
                break
            index += 1
            current = mentions[index]
            slash_group.append(current)
        if len(slash_group) > 1:
            labels = [CONCEPT_BY_KEY[item[2]].label for item in slash_group]
            keys = tuple(item[2] for item in slash_group)
            label = " / ".join(labels)
            requirements.append(RequirementGroup("role:" + "+".join(keys), label, "ambiguous", keys, "title"))
            unmodeled.append(f"Ambiguous slash requirement: {label}")
        else:
            key = slash_group[0][2]
            concept = CONCEPT_BY_KEY[key]
            requirements.append(RequirementGroup(f"role:{key}", concept.label, "all_of", (key,), "title"))
            if key in concepts:
                satisfied.append(concept.label)
                evidence.append(FitEvidence(concept.label, profile_evidence.get(key, concept.label), "profile_or_reviewed_adjacency"))
                if profile_evidence.get(key) != concept.label:
                    used_adjacencies.append(_adjacency_for_key(key))
            else:
                missing.append(concept.label)
        index += 1
    for pattern, label in UNMODELED_DEFINING_PATTERNS:
        if re.search(pattern, title) and label not in satisfied and label not in missing:
            unmodeled.append(label)
    return {
        "requirements": requirements,
        "evidence": evidence,
        "satisfied": satisfied,
        "missing": missing,
        "unmodeled": unmodeled,
        "used_adjacencies": unique([value for value in used_adjacencies if value]),
    }


def _evaluate_generic_role(profile: dict, title: str, match: dict, role_result: dict) -> dict:
    evidence = []
    satisfied = []
    unmodeled = []
    matched_languages = [normalize_text(value) for value in match.get("matched_languages") or []]
    language_role = any(re.search(pattern, title) for pattern in LANGUAGE_DATA_PATTERNS)
    if language_role and matched_languages and not _professional_language_modifier(title):
        language = matched_languages[0].title()
        evidence.append(FitEvidence("General language-data work", language, "language"))
        satisfied.append("General language-data work")

    if re.search(r"\bbilingual\b", title):
        profile_languages = unique(
            [normalize_text(value) for value in profile.get("languages") or []]
        )
        if len(profile_languages) >= 2:
            language_label = " and ".join(value.title() for value in profile_languages[:2])
            evidence.append(FitEvidence("Bilingual work", language_label, "language"))
            satisfied.append("Bilingual work")
        else:
            unmodeled.append("Two confirmed working languages")

    role_title = re.sub(r"\s*-\s*freelance\s+ai\s+trainer\s+project.*$", "", title)
    generic_ai_role = any(re.search(pattern, role_title) for pattern in GENERIC_AI_ROLE_PATTERNS)
    if generic_ai_role:
        if _profile_requests_general_ai_work(profile) and not role_result["missing"]:
            evidence.append(FitEvidence("General AI evaluation or data work", "stated AI evaluation/data-work interest", "preference"))
            satisfied.append("General AI evaluation or data work")

    if any(re.search(pattern, title) for pattern in SOFTWARE_ROLE_PATTERNS):
        if _profile_has_software_evidence(profile):
            evidence.append(FitEvidence("Software or coding work", "software engineering and declared coding skills", "structured_profile"))
            satisfied.append("Software or coding work")

    has_defining_evidence = bool(
        role_result["requirements"]
        or language_role
        or generic_ai_role
        or any(re.search(pattern, title) for pattern in SOFTWARE_ROLE_PATTERNS)
        or match.get("specialization_requirements")
    )
    if not has_defining_evidence:
        unmodeled.append("Title-defining role or specialization")
    elif language_role and not matched_languages:
        unmodeled.append("Supported language for language-data work")
    return {"evidence": evidence, "satisfied": satisfied, "unmodeled": unmodeled}


def _evaluate_location(profile: dict, title: str) -> dict:
    requirement = None
    evidence = []
    satisfied = []
    missing = []
    conflicts = []
    details = []
    location = _title_location_requirement(title)
    if not location:
        return {
            "requirement": requirement,
            "evidence": evidence,
            "satisfied": satisfied,
            "missing": missing,
            "conflicts": conflicts,
            "details": details,
        }
    key, label = location
    requirement = RequirementGroup(f"location:{key}", label, "all_of", (key,), "title_location")
    country = normalize_country(
        profile.get("country") or profile.get("location") or profile.get("residence") or ""
    )
    if not country:
        missing.append(label)
        details.append(f"{label} is required; profile location is unknown")
    elif country == key:
        satisfied.append(label)
        evidence.append(FitEvidence(label, country_label(country), "location"))
        details.append(f"Profile location satisfies {label}")
    else:
        conflicts.append(f"{label}; profile location is {country_label(country)}")
        details.append(f"Profile location conflicts with {label}")
    return {
        "requirement": requirement,
        "evidence": evidence,
        "satisfied": satisfied,
        "missing": missing,
        "conflicts": conflicts,
        "details": details,
    }


def _evaluate_title_credentials(profile: dict, title: str) -> dict:
    requirements = []
    evidence = []
    satisfied = []
    missing = []
    conflicts = []
    degree_alternatives = None
    if (
        re.search(r"\b(?:ba|bachelor)", title)
        and re.search(r"\b(?:ms|master)", title)
        and re.search(r"\bph\.?d\b", title)
    ):
        degree_alternatives = ("bachelor", "master", "doctorate")
    elif (
        re.search(r"\bph\.?d\b", title)
        and re.search(r"\bmaster", title)
    ):
        degree_alternatives = ("master", "doctorate")
    if not degree_alternatives:
        return locals_subset(requirements, evidence, satisfied, missing, conflicts)

    label_by_level = {
        "bachelor": "Bachelor's degree",
        "master": "Master's degree",
        "doctorate": "PhD or doctorate",
    }
    label = " or ".join(label_by_level[level] for level in degree_alternatives)
    requirements.append(
        RequirementGroup(
            "credential:" + "+".join(degree_alternatives),
            label,
            "any_of",
            degree_alternatives,
            "title_credential",
        )
    )
    education = normalize_text(profile.get("education_level"))
    profile_level = {
        "bachelors": "bachelor",
        "bachelor's": "bachelor",
        "masters": "master",
        "master's": "master",
        "phd": "doctorate",
    }.get(education, education)
    if profile_level in degree_alternatives:
        matched_label = label_by_level[profile_level]
        satisfied.append(label)
        evidence.append(FitEvidence(label, matched_label, "credential"))
    elif profile_level == "doctorate" and "master" in degree_alternatives:
        satisfied.append(label)
        evidence.append(FitEvidence(label, "PhD or doctorate", "credential"))
    elif profile_level == "no_degree":
        conflicts.append(label)
    else:
        missing.append(label)
    return locals_subset(requirements, evidence, satisfied, missing, conflicts)


def _title_location_requirement(title: str) -> tuple[str, str] | None:
    if re.search(r"\(\s*india\s*(?:[,;-][^)]*)?\)", title):
        return "india", "India eligibility"
    if re.search(r"\b(?:us|u s|united states)(?:[-\s]+only|[-\s]?based)\b", title):
        return "united states", "United States eligibility"
    return None


def _role_mentions(title: str) -> list[tuple[int, int, str]]:
    candidates = []
    for concept in ROLE_CONCEPTS:
        for alias in concept.aliases:
            for match in re.finditer(alias_pattern(alias), title):
                candidates.append((match.start(), match.end(), concept.key))
    candidates.sort(key=lambda value: (value[0], -(value[1] - value[0]), value[2]))
    result = []
    for candidate in candidates:
        if any(candidate[0] < item[1] and candidate[1] > item[0] for item in result):
            continue
        result.append(candidate)
    return result


def _profile_requests_general_ai_work(profile: dict) -> bool:
    text = normalize_text(
        " ".join(
            [
                str(profile.get("summary") or ""),
                " ".join(str(value) for value in profile.get("target_opportunity_types") or []),
                " ".join(str(value) for value in profile.get("skills") or []),
            ]
        )
    )
    return any(
        contains_positive_alias(text, term)
        for term in ("ai data", "ai evaluation", "ai training", "data annotation", "evaluation", "review")
    )


def _profile_has_software_evidence(profile: dict) -> bool:
    domains = normalize_text(" ".join(str(value) for value in profile.get("degrees_or_domains") or []))
    skills = normalize_text(" ".join(str(value) for value in profile.get("skills") or []))
    return contains_alias(domains, "software engineering") and any(
        contains_alias(skills, term) for term in ("python", "typescript", "javascript", "react")
    )


def _professional_language_modifier(title: str) -> bool:
    return any(
        term in title
        for term in (
            "audio specialist",
            "audio engineer",
            "audio editor",
            "voice actor",
            "voice coach",
            "writing specialist",
            "writing generalist",
        )
    )


def _why_fit_statements(title: str, evidence: list[FitEvidence], adjacencies: list[str]) -> list[str]:
    by_requirement = {item.requirement: item for item in evidence}
    if "Frontend Development" in by_requirement:
        return ["Your React and TypeScript experience aligns with this frontend role."]
    if "Backend Development" in by_requirement:
        return ["Your API and data-platform experience aligns with this backend role."]
    if "Infrastructure Engineering" in by_requirement:
        return ["Your software and data-platform experience aligns with this infrastructure role."]
    if "Full Stack Development" in by_requirement:
        return ["Your React, TypeScript, and API experience aligns with this full-stack role."]
    if "Computational Biology" in by_requirement:
        if "Research" in by_requirement:
            return ["Your computational biology and research background align with this opportunity."]
        return ["Your computational biology background aligns with this opportunity."]
    if "Biology" in by_requirement and "Research" in by_requirement:
        research_evidence = by_requirement["Research"].profile_evidence
        if "phd" in research_evidence.lower():
            return ["Your Biology PhD and research background align with this biology research opportunity."]
        return ["Your biology research background aligns with this biology research opportunity."]
    if "Biology" in by_requirement:
        return ["Your biology background aligns with this biology opportunity."]
    language_data = by_requirement.get("General language-data work")
    if language_data:
        return [f"This general language-data role matches the {language_data.profile_evidence} listed in your profile."]
    bilingual = by_requirement.get("Bilingual work")
    if bilingual:
        return [f"Your {bilingual.profile_evidence} language background aligns with this bilingual opportunity."]
    generic_ai = by_requirement.get("General AI evaluation or data work")
    if generic_ai:
        return ["Your stated interest in AI evaluation and data work aligns with this general AI-work opportunity."]
    software = by_requirement.get("Software or coding work")
    if software:
        return ["Your software engineering background and declared coding skills align with this role."]
    specific = next((item for item in evidence if item.source not in {"location", "locale", "preference"}), None)
    if specific:
        return [f"Your {specific.profile_evidence} experience aligns with this {specific.requirement} requirement."]
    return []


def _adjacency_for_key(key: str) -> str:
    return {
        "frontend_development": "React + TypeScript -> Frontend Development",
        "backend_development": "API/data-platform experience -> Backend Development",
        "infrastructure_engineering": "Data-platform experience -> Infrastructure Engineering",
        "full_stack_development": "Frontend + backend evidence -> Full Stack Development",
        "research": "Biology + research evidence -> Biology Research",
    }.get(key, "")


def _cap_reason_label(reason: str) -> str:
    return {
        "personalized_eligibility_failed": "Existing personalized eligibility gate",
        "professional_domain_hard_gate": "Existing professional-domain hard gate",
        "explicit_credential_incompatibility": "Explicit credential incompatibility",
        "unsupported_title_language_or_dialect": "Unsupported required language or dialect",
        "incompatible_location": "Explicit location incompatibility",
    }.get(reason, reason.replace("_", " "))


def _display_concept(value: str) -> str:
    return value.replace("_", " ").title()


def normalize_country(value: str) -> str:
    text = normalize_text(value)
    if text in {"us", "u s", "usa", "united states", "united states of america"}:
        return "united states"
    if text == "india" or re.search(r"\bindia\b", text):
        return "india"
    return text


def country_label(value: str) -> str:
    return "United States" if value == "united states" else value.title()


def contains_alias(text: str, alias: str) -> bool:
    return re.search(alias_pattern(alias), text) is not None


def contains_positive_alias(text: str, alias: str) -> bool:
    return any(
        not term_is_negated(text, match.start(), match.end())
        for match in re.finditer(alias_pattern(alias), text)
    )


def alias_pattern(alias: str) -> str:
    normalized = normalize_text(alias)
    escaped = r"\s+".join(re.escape(part) for part in normalized.split())
    return rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"


def normalize_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"[^a-z0-9#+/&'().,:;-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _unique_requirements(values: list[RequirementGroup]) -> list[RequirementGroup]:
    result = []
    seen = set()
    for value in values:
        if value.key in seen:
            continue
        seen.add(value.key)
        result.append(value)
    return result


def _unique_evidence(values: list[FitEvidence]) -> list[FitEvidence]:
    result = []
    seen = set()
    for value in values:
        key = (value.requirement.lower(), value.profile_evidence.lower(), value.source)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def unique(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def locals_subset(requirements, evidence, satisfied, missing, conflicts=None) -> dict:
    return {
        "requirements": requirements,
        "evidence": evidence,
        "satisfied": satisfied,
        "missing": missing,
        "conflicts": conflicts or [],
    }

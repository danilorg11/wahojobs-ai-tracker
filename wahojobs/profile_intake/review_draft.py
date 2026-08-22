"""Pure transformation from validated extraction to an ephemeral review draft."""

from __future__ import annotations

from dataclasses import dataclass

from wahojobs.profile_intake.contracts import (
    AIProfileExtraction,
    ExtractedFact,
    INFERRED_ONLY_EXTRACTION_FIELD_PATHS,
    LanguageValue,
    ProfileIntakeError,
)


REVIEW_DRAFT_SCHEMA_VERSION = "ai_profile_review_draft_v1"

_REVIEW_FIELDS = {
    "identity.display_name": "display_name",
    "languages": "languages",
    "location.country": "country",
    "location.region": "region",
    "location.city": "city",
    "location.residence": "country",
    "education.education_level": "education_level",
    "education.degrees": "degrees",
    "education.fields_or_domains": "education_fields",
    "education.institutions": "institutions",
    "education.completion_status": "education_status",
    "credentials.certifications": "certifications",
    "credentials.licenses": "licenses",
    "credentials.jurisdictions": "jurisdictions",
    "credentials.security_clearances": "security_clearances",
    "credentials.credential_status": "credential_status",
    "experience.total_years": "total_years",
    "experience.seniority": "seniority",
    "experience.recent_roles": "job_titles",
    "experience.occupational_families": "occupational_families",
    "experience.job_titles": "job_titles",
    "experience.professional_domains": "professional_domains",
    "experience.industries": "industries",
    "experience.contribution_type": "contribution_type",
    "experience.specialties": "specialties",
    "skills.normalized": "skills",
    "preferences.remote": "remote",
    "preferences.flexible": "flexible",
    "preferences.employment_types": "employment_types",
    "preferences.synchronous_preference": "synchronous_preference",
    "preferences.phone_preference": "phone_preference",
    "preferences.schedule": "schedule",
    "preferences.availability": "availability",
    "preferences.target_opportunity_types": "target_opportunity_types",
    "preferences.preferred_task_types": "target_opportunity_types",
    "preferences.work_preferences": "work_preferences",
}

# These remain questions for the user. An explicitly stated present remote or
# flexible preference may be shown as a suggestion; historical behavior never
# removes one from this list.
_USER_ONLY_REVIEW_FIELDS = (
    "work_authorization",
    "eligible_countries",
    "geographic_restrictions",
    "remote",
    "flexible",
    "employment_types",
    "synchronous_preference",
    "phone_preference",
    "schedule",
    "availability",
    "target_opportunity_types",
    "work_preferences",
    "hard_constraints",
    "soft_preferences",
    "avoid_keywords",
    "excluded_domains",
    "accessibility_constraints",
)


@dataclass(frozen=True, slots=True)
class ReviewDraftFact:
    field_path: str
    review_field: str
    value: str | int | float | bool | LanguageValue
    evidence_block_references: tuple[str, ...]
    confidence: float
    explicit: bool
    requires_confirmation: bool


@dataclass(frozen=True, slots=True)
class ReviewDraftIssue:
    kind: str
    field_path: str
    evidence_block_references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AIProfileReviewDraft:
    schema_version: str
    document_reference: str
    prefilled_facts: tuple[ReviewDraftFact, ...]
    suggested_facts: tuple[ReviewDraftFact, ...]
    missing_user_fields: tuple[str, ...]
    issues: tuple[ReviewDraftIssue, ...]


def _value_sort_key(value: object) -> str:
    if type(value) is LanguageValue:
        return "|".join(
            (value.language.casefold(), value.proficiency or "", value.locale or "")
        )
    return repr(value)


def _fact_sort_key(fact: ExtractedFact) -> tuple[str, str, tuple[str, ...]]:
    return fact.field_path, _value_sort_key(fact.value), fact.evidence_block_references


def _is_suggestion(fact: ExtractedFact) -> bool:
    return (
        not fact.explicit
        or fact.field_path in INFERRED_ONLY_EXTRACTION_FIELD_PATHS
        or fact.field_path == "experience.total_years"
        or fact.field_path.startswith("preferences.")
    )


def _draft_fact(fact: ExtractedFact, *, suggestion: bool) -> ReviewDraftFact:
    return ReviewDraftFact(
        field_path=fact.field_path,
        review_field=_REVIEW_FIELDS[fact.field_path],
        value=fact.value,
        evidence_block_references=fact.evidence_block_references,
        confidence=fact.confidence,
        explicit=fact.explicit,
        requires_confirmation=suggestion,
    )


def _issues(facts: tuple[ExtractedFact, ...]) -> tuple[ReviewDraftIssue, ...]:
    issues: list[ReviewDraftIssue] = []
    for fact in facts:
        if fact.confidence < 0.6:
            issues.append(
                ReviewDraftIssue(
                    kind="ambiguous_low_confidence",
                    field_path=fact.field_path,
                    evidence_block_references=fact.evidence_block_references,
                )
            )

    languages: dict[str, list[ExtractedFact]] = {}
    for fact in facts:
        if fact.field_path == "languages" and type(fact.value) is LanguageValue:
            languages.setdefault(fact.value.language.casefold(), []).append(fact)
    for language_facts in languages.values():
        variants = {
            (fact.value.proficiency, fact.value.locale)
            for fact in language_facts
            if type(fact.value) is LanguageValue
        }
        if len(variants) > 1:
            references = tuple(
                sorted(
                    {
                        reference
                        for fact in language_facts
                        for reference in fact.evidence_block_references
                    }
                )
            )
            issues.append(
                ReviewDraftIssue(
                    kind="conflicting_language_detail",
                    field_path="languages",
                    evidence_block_references=references,
                )
            )
    return tuple(
        sorted(issues, key=lambda issue: (issue.field_path, issue.kind, issue.evidence_block_references))
    )


def build_profile_review_draft(extraction: AIProfileExtraction) -> AIProfileReviewDraft:
    """Build a deterministic in-memory draft with no persistence authority."""

    if type(extraction) is not AIProfileExtraction:
        raise ProfileIntakeError("invalid_profile_extraction")
    facts = tuple(sorted(extraction.facts, key=_fact_sort_key))
    prefilled: list[ReviewDraftFact] = []
    suggested: list[ReviewDraftFact] = []
    explicitly_present_user_fields: set[str] = set()
    for fact in facts:
        suggestion = _is_suggestion(fact)
        draft_fact = _draft_fact(fact, suggestion=suggestion)
        (suggested if suggestion else prefilled).append(draft_fact)
        if fact.explicit and fact.field_path in {
            "preferences.remote",
            "preferences.flexible",
        }:
            explicitly_present_user_fields.add(draft_fact.review_field)
    missing_user_fields = tuple(
        field
        for field in _USER_ONLY_REVIEW_FIELDS
        if field not in explicitly_present_user_fields
    )
    return AIProfileReviewDraft(
        schema_version=REVIEW_DRAFT_SCHEMA_VERSION,
        document_reference=extraction.document_reference,
        prefilled_facts=tuple(prefilled),
        suggested_facts=tuple(suggested),
        missing_user_fields=missing_user_fields,
        issues=_issues(facts),
    )

"""Pure transformations from validated extractions to one ephemeral review draft."""

from __future__ import annotations

from dataclasses import dataclass
import math

from wahojobs.profile_intake.contracts import (
    AIProfileExtraction,
    DocumentKind,
    ExtractedFact,
    INFERRED_ONLY_EXTRACTION_FIELD_PATHS,
    LanguageValue,
    ProfileIntakeError,
    _FIELD_SPECS,
    _EVIDENCE_REFERENCE,
    _require_document_reference,
    _validate_fact_value,
)


REVIEW_DRAFT_SCHEMA_VERSION = "ai_profile_review_draft_v2"

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
    "education.graduation_years": "graduation_years",
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
class ValidatedProfileSource:
    """One independently validated extraction and its importer-only origin."""

    document_kind: DocumentKind
    extraction: AIProfileExtraction

    def __post_init__(self):
        if (
            type(self.document_kind) is not DocumentKind
            or type(self.extraction) is not AIProfileExtraction
        ):
            raise ProfileIntakeError("invalid_profile_reconciliation_source")


@dataclass(frozen=True, slots=True)
class ReviewDraftSource:
    document_reference: str
    document_kind: DocumentKind

    def __post_init__(self):
        _require_document_reference(self.document_reference)
        if type(self.document_kind) is not DocumentKind:
            raise ProfileIntakeError("invalid_profile_reconciliation_source")


@dataclass(frozen=True, slots=True)
class ReviewSourceAttribution:
    """Scoped evidence identity; block IDs are never ambiguous across sources."""

    document_reference: str
    document_kind: DocumentKind
    evidence_block_references: tuple[str, ...]

    def __post_init__(self):
        _require_document_reference(self.document_reference)
        if (
            type(self.document_kind) is not DocumentKind
            or type(self.evidence_block_references) is not tuple
            or not 1 <= len(self.evidence_block_references) <= 16
            or len(set(self.evidence_block_references))
            != len(self.evidence_block_references)
            or tuple(sorted(self.evidence_block_references))
            != self.evidence_block_references
            or any(
                type(reference) is not str
                or _EVIDENCE_REFERENCE.fullmatch(reference) is None
                for reference in self.evidence_block_references
            )
        ):
            raise ProfileIntakeError("invalid_review_source_attribution")


@dataclass(frozen=True, slots=True)
class ReviewDraftFact:
    field_path: str
    review_field: str
    value: str | int | float | bool | LanguageValue
    source_attributions: tuple[ReviewSourceAttribution, ...]
    confidence: float
    explicit: bool
    requires_confirmation: bool
    conflict_group: str | None = None

    def __post_init__(self):
        spec = _FIELD_SPECS.get(self.field_path)
        if (
            spec is None
            or self.review_field != _REVIEW_FIELDS.get(self.field_path)
            or _validate_fact_value(self.value, spec) != self.value
            or type(self.source_attributions) is not tuple
            or not 1 <= len(self.source_attributions) <= 2
            or any(
                type(item) is not ReviewSourceAttribution
                for item in self.source_attributions
            )
            or len(
                {item.document_reference for item in self.source_attributions}
            )
            != len(self.source_attributions)
            or type(self.confidence) is not float
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
            or type(self.explicit) is not bool
            or type(self.requires_confirmation) is not bool
            or (
                self.conflict_group is not None
                and (
                    type(self.conflict_group) is not str
                    or not self.conflict_group
                    or len(self.conflict_group) > 640
                    or "\x00" in self.conflict_group
                )
            )
            or (self.conflict_group is not None and not self.requires_confirmation)
        ):
            raise ProfileIntakeError("invalid_review_draft_fact")


@dataclass(frozen=True, slots=True)
class ReviewDraftIssue:
    kind: str
    field_path: str
    source_attributions: tuple[ReviewSourceAttribution, ...]

    def __post_init__(self):
        if (
            type(self.kind) is not str
            or not self.kind
            or self.field_path not in _FIELD_SPECS
            or type(self.source_attributions) is not tuple
            or not 1 <= len(self.source_attributions) <= 2
            or any(
                type(item) is not ReviewSourceAttribution
                for item in self.source_attributions
            )
        ):
            raise ProfileIntakeError("invalid_review_draft_issue")


@dataclass(frozen=True, slots=True)
class AIProfileReviewDraft:
    schema_version: str
    sources: tuple[ReviewDraftSource, ...]
    prefilled_facts: tuple[ReviewDraftFact, ...]
    suggested_facts: tuple[ReviewDraftFact, ...]
    missing_user_fields: tuple[str, ...]
    issues: tuple[ReviewDraftIssue, ...]

    def __post_init__(self):
        if (
            self.schema_version != REVIEW_DRAFT_SCHEMA_VERSION
            or type(self.sources) is not tuple
            or not 1 <= len(self.sources) <= 2
            or any(type(source) is not ReviewDraftSource for source in self.sources)
            or len({source.document_kind for source in self.sources})
            != len(self.sources)
            or len({source.document_reference for source in self.sources})
            != len(self.sources)
            or any(
                type(fact) is not ReviewDraftFact
                for fact in (*self.prefilled_facts, *self.suggested_facts)
            )
            or any(type(issue) is not ReviewDraftIssue for issue in self.issues)
            or type(self.missing_user_fields) is not tuple
            or any(type(name) is not str for name in self.missing_user_fields)
        ):
            raise ProfileIntakeError("invalid_profile_review_draft")
        source_map = {
            source.document_reference: source.document_kind
            for source in self.sources
        }
        for fact in (*self.prefilled_facts, *self.suggested_facts):
            if any(
                source_map.get(item.document_reference) is not item.document_kind
                for item in fact.source_attributions
            ):
                raise ProfileIntakeError("invalid_profile_review_draft")
        for issue in self.issues:
            if any(
                source_map.get(item.document_reference) is not item.document_kind
                for item in issue.source_attributions
            ):
                raise ProfileIntakeError("invalid_profile_review_draft")

    @property
    def document_reference(self):
        """Compatibility accessor for the unchanged single-document path."""

        return self.sources[0].document_reference if len(self.sources) == 1 else None


def _text_identity(value: str | None):
    return None if value is None else value.casefold()


def _value_identity(value: object) -> object:
    if type(value) is LanguageValue:
        return (
            value.language.casefold(),
            _text_identity(value.proficiency),
            _text_identity(value.locale),
        )
    if type(value) is str:
        return value.casefold()
    if type(value) in (int, float):
        return float(value)
    return value


def _value_sort_key(value: object) -> tuple[str, ...]:
    if type(value) is LanguageValue:
        return (
            "language",
            value.language.casefold(),
            value.language,
            value.proficiency or "",
            value.locale or "",
        )
    if type(value) is str:
        return "string", value.casefold(), value
    if type(value) is bool:
        return "boolean", "1" if value else "0"
    return "number", repr(float(value))


def _fact_sort_key(fact: ReviewDraftFact):
    return (
        fact.field_path,
        fact.conflict_group or "",
        _value_sort_key(fact.value),
        tuple(
            (
                attribution.document_kind.value,
                attribution.document_reference,
                attribution.evidence_block_references,
            )
            for attribution in fact.source_attributions
        ),
    )


def _is_suggestion(field_path: str, explicit: bool) -> bool:
    return (
        not explicit
        or field_path in INFERRED_ONLY_EXTRACTION_FIELD_PATHS
        or field_path == "experience.total_years"
        or field_path.startswith("preferences.")
    )


def _source_attributions(
    facts: tuple[ExtractedFact, ...],
    source_kinds: dict[str, DocumentKind],
) -> tuple[ReviewSourceAttribution, ...]:
    references: dict[str, set[str]] = {}
    for fact in facts:
        references.setdefault(fact.source_document_reference, set()).update(
            fact.evidence_block_references
        )
    return tuple(
        ReviewSourceAttribution(
            document_reference=document_reference,
            document_kind=source_kinds[document_reference],
            evidence_block_references=tuple(sorted(blocks)),
        )
        for document_reference, blocks in sorted(
            references.items(),
            key=lambda item: (
                0 if source_kinds[item[0]] is DocumentKind.RESUME else 1,
                item[0],
            ),
        )
    )


def _merge_equal_facts(
    facts: tuple[ExtractedFact, ...],
    source_kinds: dict[str, DocumentKind],
    *,
    conflict_group: str | None,
) -> ReviewDraftFact:
    representative = min(facts, key=lambda fact: _value_sort_key(fact.value))
    explicit = any(fact.explicit for fact in facts)
    requires_confirmation = conflict_group is not None or _is_suggestion(
        representative.field_path,
        explicit,
    )
    return ReviewDraftFact(
        field_path=representative.field_path,
        review_field=_REVIEW_FIELDS[representative.field_path],
        value=representative.value,
        source_attributions=_source_attributions(facts, source_kinds),
        confidence=max(fact.confidence for fact in facts),
        explicit=explicit,
        requires_confirmation=requires_confirmation,
        conflict_group=conflict_group,
    )


def _reconcile_field(field_path, facts, source_kinds):
    spec = _FIELD_SPECS[field_path]
    grouped: dict[object, list[ExtractedFact]] = {}
    for fact in facts:
        grouped.setdefault(_value_identity(fact.value), []).append(fact)

    conflict_group = None
    issue_kind = None
    if not spec.multiple and len(grouped) > 1:
        conflict_group = field_path
        issue_kind = "conflicting_source_values"

    merged: list[ReviewDraftFact] = []
    issues: list[ReviewDraftIssue] = []
    for identity in sorted(grouped, key=repr):
        equal_facts = tuple(grouped[identity])
        merged.append(
            _merge_equal_facts(
                equal_facts,
                source_kinds,
                conflict_group=conflict_group,
            )
        )

    if issue_kind is not None:
        issues.append(
            ReviewDraftIssue(
                kind=issue_kind,
                field_path=field_path,
                source_attributions=_source_attributions(tuple(facts), source_kinds),
            )
        )
    for fact in merged:
        if fact.confidence < 0.6:
            issues.append(
                ReviewDraftIssue(
                    kind="ambiguous_low_confidence",
                    field_path=field_path,
                    source_attributions=fact.source_attributions,
                )
            )
    return merged, issues


def _reconcile_languages(facts, source_kinds):
    by_language: dict[str, list[ExtractedFact]] = {}
    for fact in facts:
        if type(fact.value) is not LanguageValue:
            raise ProfileIntakeError("invalid_profile_reconciliation_fact")
        by_language.setdefault(fact.value.language.casefold(), []).append(fact)

    merged: list[ReviewDraftFact] = []
    issues: list[ReviewDraftIssue] = []
    for language in sorted(by_language):
        language_facts = by_language[language]
        variants: dict[object, list[ExtractedFact]] = {}
        for fact in language_facts:
            variants.setdefault(_value_identity(fact.value), []).append(fact)
        conflict_group = f"languages:{language}" if len(variants) > 1 else None
        for identity in sorted(variants, key=repr):
            fact = _merge_equal_facts(
                tuple(variants[identity]),
                source_kinds,
                conflict_group=conflict_group,
            )
            merged.append(fact)
            if fact.confidence < 0.6:
                issues.append(
                    ReviewDraftIssue(
                        kind="ambiguous_low_confidence",
                        field_path="languages",
                        source_attributions=fact.source_attributions,
                    )
                )
        if conflict_group is not None:
            issues.append(
                ReviewDraftIssue(
                    kind="conflicting_language_detail",
                    field_path="languages",
                    source_attributions=_source_attributions(
                        tuple(language_facts), source_kinds
                    ),
                )
            )
    return merged, issues


def reconcile_profile_extractions(
    sources: tuple[ValidatedProfileSource, ...],
) -> AIProfileReviewDraft:
    """Reconcile one or two independently validated sources without precedence."""

    if (
        type(sources) is not tuple
        or not 1 <= len(sources) <= 2
        or any(type(source) is not ValidatedProfileSource for source in sources)
        or len({source.document_kind for source in sources}) != len(sources)
        or len({source.extraction.document_reference for source in sources}) != len(sources)
    ):
        raise ProfileIntakeError("invalid_profile_reconciliation_sources")
    ordered_sources = tuple(
        sorted(
            sources,
            key=lambda source: (
                0 if source.document_kind is DocumentKind.RESUME else 1,
                source.extraction.document_reference,
            ),
        )
    )
    source_kinds = {
        source.extraction.document_reference: source.document_kind
        for source in ordered_sources
    }
    facts_by_field: dict[str, list[ExtractedFact]] = {}
    for source in ordered_sources:
        for fact in source.extraction.facts:
            facts_by_field.setdefault(fact.field_path, []).append(fact)

    facts: list[ReviewDraftFact] = []
    issues: list[ReviewDraftIssue] = []
    for field_path in sorted(facts_by_field):
        if field_path == "languages":
            field_results, field_issues = _reconcile_languages(
                facts_by_field[field_path], source_kinds
            )
        else:
            field_results, field_issues = _reconcile_field(
                field_path, facts_by_field[field_path], source_kinds
            )
        facts.extend(field_results)
        issues.extend(field_issues)

    facts = sorted(facts, key=_fact_sort_key)
    prefilled = tuple(fact for fact in facts if not fact.requires_confirmation)
    suggested = tuple(fact for fact in facts if fact.requires_confirmation)
    explicitly_present_user_fields = {
        fact.review_field
        for fact in facts
        if fact.explicit
        and fact.conflict_group is None
        and fact.field_path in {"preferences.remote", "preferences.flexible"}
    }
    missing_user_fields = tuple(
        field
        for field in _USER_ONLY_REVIEW_FIELDS
        if field not in explicitly_present_user_fields
    )
    return AIProfileReviewDraft(
        schema_version=REVIEW_DRAFT_SCHEMA_VERSION,
        sources=tuple(
            ReviewDraftSource(
                document_reference=source.extraction.document_reference,
                document_kind=source.document_kind,
            )
            for source in ordered_sources
        ),
        prefilled_facts=prefilled,
        suggested_facts=suggested,
        missing_user_fields=missing_user_fields,
        issues=tuple(
            sorted(
                issues,
                key=lambda issue: (
                    issue.field_path,
                    issue.kind,
                    tuple(
                        (
                            attribution.document_kind.value,
                            attribution.document_reference,
                            attribution.evidence_block_references,
                        )
                        for attribution in issue.source_attributions
                    ),
                ),
            )
        ),
    )


def build_profile_review_draft(
    extraction: AIProfileExtraction,
    *,
    document_kind: DocumentKind = DocumentKind.RESUME,
) -> AIProfileReviewDraft:
    """Backward-compatible single-source transformation."""

    if type(extraction) is not AIProfileExtraction:
        raise ProfileIntakeError("invalid_profile_extraction")
    return reconcile_profile_extractions(
        (ValidatedProfileSource(document_kind=document_kind, extraction=extraction),)
    )

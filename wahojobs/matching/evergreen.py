from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from wahojobs.classification import (
    INVENTORY_MODEL_EVERGREEN_APPLICATION,
    MARKET_COUNT_POLICY_REPORT_SEPARATELY,
    OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
)
from wahojobs.matching.languages import (
    REQUIREMENT_AMBIGUOUS,
    REQUIREMENT_NONE,
    LanguageEligibility,
    language_eligibility,
    profile_language_set,
)


EVERGREEN_KIND_GENERALIST = "broad_generalist"
EVERGREEN_KIND_LANGUAGE = "broad_language"
EVERGREEN_KIND_GENERALIST_LANGUAGE = "broad_generalist_language"
EVERGREEN_KIND_SPECIALIST = "specialist"
EVERGREEN_KIND_UNKNOWN = "unknown"

EVERGREEN_REASON = "This is a broad evergreen application opportunity worth keeping in your application pipeline."


@dataclass(frozen=True)
class EvergreenApplicability:
    qualifies: bool
    opportunity_kind: str
    profile_kind: str
    reason: str


def evergreen_applicability(
    profile: dict,
    row: dict,
    language_check: LanguageEligibility,
) -> EvergreenApplicability:
    opportunity_kind = evergreen_opportunity_kind(row)
    profile_kind = evergreen_profile_kind(profile)

    if not is_evergreen_application(row):
        return result(False, opportunity_kind, profile_kind, "Opportunity is not an evergreen application.")

    if opportunity_kind == EVERGREEN_KIND_UNKNOWN:
        return result(False, opportunity_kind, profile_kind, "Structured evergreen category is unknown.")

    if opportunity_kind == EVERGREEN_KIND_SPECIALIST:
        return result(False, opportunity_kind, profile_kind, "Evergreen opportunity is specialist-specific.")

    if opportunity_kind == EVERGREEN_KIND_GENERALIST and profile_kind not in {
        EVERGREEN_KIND_GENERALIST,
        EVERGREEN_KIND_GENERALIST_LANGUAGE,
    }:
        return result(False, opportunity_kind, profile_kind, "Profile is not a broad application fit.")

    if opportunity_kind == EVERGREEN_KIND_LANGUAGE:
        if profile_kind not in {EVERGREEN_KIND_LANGUAGE, EVERGREEN_KIND_GENERALIST_LANGUAGE}:
            return result(False, opportunity_kind, profile_kind, "Profile is not language-oriented.")
        structured_language_check = language_eligibility(profile, structured_language_text(row))
        if not language_compatible_for_evergreen(structured_language_check):
            return result(False, opportunity_kind, profile_kind, structured_language_check.reason)

    return result(True, opportunity_kind, profile_kind, EVERGREEN_REASON)


def is_evergreen_application(row: dict) -> bool:
    return (
        row.get("inventory_model") == INVENTORY_MODEL_EVERGREEN_APPLICATION
        and row.get("opportunity_kind") == OPPORTUNITY_KIND_EVERGREEN_APPLICATION
        and row.get("market_count_policy") == MARKET_COUNT_POLICY_REPORT_SEPARATELY
    )


def evergreen_opportunity_kind(row: dict) -> str:
    category = structured_category_text(row)
    if not category or category == "unknown":
        return EVERGREEN_KIND_UNKNOWN
    if category in {"generalist", "generalist ai trainer"}:
        return EVERGREEN_KIND_GENERALIST
    if category in {
        "bilingual",
        "language",
        "language experts",
        "language linguistics",
        "language and linguistics",
        "linguistics",
    }:
        return EVERGREEN_KIND_LANGUAGE
    return EVERGREEN_KIND_SPECIALIST


def evergreen_profile_kind(profile: dict) -> str:
    text = normalize(
        " ".join(
            [
                profile.get("profile_id", ""),
                profile.get("display_name", ""),
                profile.get("summary", ""),
                profile.get("education_level", ""),
                " ".join(profile.get("degrees_or_domains") or []),
                " ".join(profile.get("skills") or []),
                " ".join(profile.get("target_opportunity_types") or []),
                profile.get("notes", ""),
            ]
        )
    )
    language_count = len(profile_language_set(profile))
    language_profile = language_count > 1 or contains_any(
        text,
        (
            "bilingual",
            "language",
            "linguistic",
            "linguistics",
            "translation",
            "translator",
            "localization",
        ),
    )
    generalist_profile = contains_any(
        text,
        (
            "generalist",
            "no degree",
            "no college degree",
            "data annotation",
            "search evaluation",
            "web research",
            "content review",
            "academic writing",
            "source evaluation",
            "fact checking",
            "teaching",
            "education",
        ),
    )
    if language_profile and generalist_profile:
        return EVERGREEN_KIND_GENERALIST_LANGUAGE
    if language_profile:
        return EVERGREEN_KIND_LANGUAGE
    if generalist_profile:
        return EVERGREEN_KIND_GENERALIST
    return EVERGREEN_KIND_UNKNOWN


def language_compatible_for_evergreen(language_check: LanguageEligibility) -> bool:
    if not language_check.eligible_for_personalized:
        return False
    if language_check.requirement_mode == REQUIREMENT_AMBIGUOUS:
        return False
    if language_check.requirement_mode == REQUIREMENT_NONE:
        return True
    return bool(language_check.matched_languages)


def structured_category_text(row: dict) -> str:
    values = [
        row.get("source_category"),
        row.get("expertise"),
        row.get("department"),
    ]
    normalized = [normalize(value) for value in values if normalize(value)]
    return normalized[0] if normalized else ""


def structured_language_text(row: dict) -> str:
    values = [
        row.get("source_category"),
        row.get("expertise"),
        row.get("department"),
        row.get("commitment"),
    ]
    return " ".join(str(value or "") for value in values)


def normalize(value: str | None) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[\u2010-\u2015/]+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) for term in terms)


def result(qualifies: bool, opportunity_kind: str, profile_kind: str, reason: str) -> EvergreenApplicability:
    return EvergreenApplicability(
        qualifies=qualifies,
        opportunity_kind=opportunity_kind,
        profile_kind=profile_kind,
        reason=reason,
    )

"""Shared presentation metadata for candidate seniority and target career levels.

The persisted candidate and opportunity taxonomies remain authoritative.  This
module only groups legacy-equivalent candidate labels for human-facing choices
and supplies display copy for both taxonomies.
"""

from __future__ import annotations

from dataclasses import dataclass

from wahojobs.matching.taxonomy import CAREER_LEVELS
from wahojobs.profiles.canonical import SENIORITY_LEVELS


@dataclass(frozen=True)
class CandidateSeniorityDisplayGroup:
    key: str
    values: tuple[str, ...]
    preferred_value: str
    label: str
    description: str
    primary: bool = False


@dataclass(frozen=True)
class TargetCareerLevelDisplayChoice:
    value: str
    label: str
    description: str


CANDIDATE_SENIORITY_DISPLAY_GROUPS = (
    CandidateSeniorityDisplayGroup(
        "student",
        ("student",),
        "student",
        "Student / pre-career",
        "Currently studying or preparing for a first professional role.",
    ),
    CandidateSeniorityDisplayGroup(
        "entry_level",
        ("entry-level", "junior"),
        "entry-level",
        "Entry-level",
        "Early-career work with limited prior experience expected.",
        True,
    ),
    CandidateSeniorityDisplayGroup(
        "mid_level",
        ("mid", "mid-level"),
        "mid",
        "Mid-level",
        "Established experience working independently in the role.",
        True,
    ),
    CandidateSeniorityDisplayGroup(
        "senior",
        ("senior",),
        "senior",
        "Senior",
        "High-responsibility work requiring deep independent experience.",
        True,
    ),
    CandidateSeniorityDisplayGroup(
        "lead",
        ("lead",),
        "lead",
        "Lead",
        "Technical or functional leadership responsibility.",
        True,
    ),
    CandidateSeniorityDisplayGroup(
        "principal",
        ("principal",),
        "principal",
        "Principal",
        "High-scope expert individual-contributor responsibility.",
    ),
    CandidateSeniorityDisplayGroup(
        "executive",
        ("executive",),
        "executive",
        "Executive",
        "Organization-level leadership and decision-making responsibility.",
        True,
    ),
    CandidateSeniorityDisplayGroup(
        "advanced_specialist",
        ("advanced",),
        "advanced",
        "Advanced specialist",
        "Deep subject-matter expertise; this does not by itself mean senior leadership.",
    ),
    CandidateSeniorityDisplayGroup(
        "unknown",
        ("unknown",),
        "unknown",
        "Not sure",
        "There is not enough information to choose a career stage.",
    ),
)

TARGET_CAREER_LEVEL_DISPLAY_CHOICES = (
    TargetCareerLevelDisplayChoice(
        "internship", "Internship", "A structured learning or early-career placement."
    ),
    TargetCareerLevelDisplayChoice(
        "entry", "Entry-level", "Roles with limited prior experience expected."
    ),
    TargetCareerLevelDisplayChoice(
        "mid", "Mid-level", "Roles requiring established independent experience."
    ),
    TargetCareerLevelDisplayChoice(
        "senior", "Senior", "Senior individual-contributor responsibility."
    ),
    TargetCareerLevelDisplayChoice(
        "lead", "Lead", "Technical or functional leadership responsibility."
    ),
    TargetCareerLevelDisplayChoice(
        "principal",
        "Principal",
        "High-scope expert individual-contributor responsibility.",
    ),
    TargetCareerLevelDisplayChoice(
        "manager", "Manager", "People-management responsibility."
    ),
)

_CANDIDATE_GROUP_BY_VALUE = {
    value: group
    for group in CANDIDATE_SENIORITY_DISPLAY_GROUPS
    for value in group.values
}
_TARGET_CHOICE_BY_VALUE = {
    choice.value: choice for choice in TARGET_CAREER_LEVEL_DISPLAY_CHOICES
}

if set(_CANDIDATE_GROUP_BY_VALUE) != set(SENIORITY_LEVELS):
    raise RuntimeError("candidate_seniority_presentation_contract_mismatch")
if set(_TARGET_CHOICE_BY_VALUE) != set(CAREER_LEVELS):
    raise RuntimeError("target_career_level_presentation_contract_mismatch")
if any(
    group.preferred_value not in group.values
    for group in CANDIDATE_SENIORITY_DISPLAY_GROUPS
):
    raise RuntimeError("candidate_seniority_preferred_value_mismatch")


def candidate_seniority_display_choices(
    current_value: str | None = None,
) -> tuple[dict, ...]:
    """Return one UI choice per display group while preserving a current raw alias."""

    return tuple(
        {
            "key": group.key,
            "value": (
                current_value
                if current_value in group.values
                else group.preferred_value
            ),
            "values": group.values,
            "label": group.label,
            "description": group.description,
            "primary": group.primary,
        }
        for group in CANDIDATE_SENIORITY_DISPLAY_GROUPS
    )


def candidate_seniority_display_label(value: str) -> str:
    """Return the shared display label without rewriting the persisted value."""

    group = _CANDIDATE_GROUP_BY_VALUE.get(value)
    return group.label if group is not None else value


def target_career_level_display_choices() -> tuple[dict, ...]:
    return tuple(
        {
            "code": choice.value,
            "label": choice.label,
            "description": choice.description,
        }
        for choice in TARGET_CAREER_LEVEL_DISPLAY_CHOICES
    )


def target_career_level_display_label(value: str) -> str:
    choice = _TARGET_CHOICE_BY_VALUE.get(value)
    return choice.label if choice is not None else value

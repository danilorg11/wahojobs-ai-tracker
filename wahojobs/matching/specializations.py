"""Conservative title specialization requirements for preview actionability."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from wahojobs.profiles.normalizer import term_is_negated


@dataclass(frozen=True)
class SpecializationConcept:
    key: str
    label: str
    aliases: tuple[str, ...]
    implies: tuple[str, ...] = ()
    raw_evidence_aliases: tuple[str, ...] = ()


CONCEPTS = (
    SpecializationConcept("python", "Python", ("python",)),
    SpecializationConcept("java", "Java", ("java",)),
    SpecializationConcept("kotlin", "Kotlin", ("kotlin",)),
    SpecializationConcept("c_sharp", "C#", ("c#", "c sharp")),
    SpecializationConcept("c_plus_plus", "C++", ("c++", "c plus plus")),
    SpecializationConcept(
        "c",
        "C",
        ("c", "c language"),
        raw_evidence_aliases=("c language", "c programming", "c developer"),
    ),
    SpecializationConcept("javascript", "JavaScript", ("javascript", "js")),
    SpecializationConcept("typescript", "TypeScript", ("typescript", "ts")),
    SpecializationConcept("react", "React", ("react", "reactjs", "react.js")),
    SpecializationConcept("rust", "Rust", ("rust",)),
    SpecializationConcept(
        "go",
        "Go",
        ("go", "golang", "go language"),
        raw_evidence_aliases=("golang", "go language", "go programming", "go developer"),
    ),
    SpecializationConcept("swift", "Swift", ("swift",)),
    SpecializationConcept("ruby", "Ruby", ("ruby",)),
    SpecializationConcept("php", "PHP", ("php",)),
    SpecializationConcept("scala", "Scala", ("scala",)),
    SpecializationConcept(
        "r",
        "R",
        ("r", "r language"),
        raw_evidence_aliases=("r language", "r programming", "r developer"),
    ),
    SpecializationConcept("matlab", "MATLAB", ("matlab",)),
    SpecializationConcept("three_d_modeling", "3D Modeling", ("3d modeling", "3 d modeling")),
    SpecializationConcept("cad", "CAD", ("cad", "computer aided design")),
    SpecializationConcept("biology", "Biology", ("biology", "biologist")),
    SpecializationConcept(
        "microbiology",
        "Microbiology",
        ("microbiology", "microbiologist"),
        implies=("biology",),
    ),
    SpecializationConcept(
        "computational_biology",
        "Computational Biology",
        ("computational biology",),
        implies=("biology",),
    ),
    SpecializationConcept("drug_discovery", "Drug Discovery", ("drug discovery",)),
    SpecializationConcept("environmental_science", "Environmental Science", ("environmental science",)),
    SpecializationConcept("radiological_health", "Radiological Health", ("radiological health",)),
    SpecializationConcept("medicine", "Medicine", ("medicine", "medical")),
    SpecializationConcept("clinical_research", "Clinical Research", ("clinical research",)),
    SpecializationConcept("chemistry", "Chemistry", ("chemistry", "chemical engineering")),
    SpecializationConcept("physics", "Physics", ("physics",)),
    SpecializationConcept(
        "materials_science",
        "Materials Science",
        ("materials science", "material science"),
    ),
    SpecializationConcept("neuroscience", "Neuroscience", ("neuroscience",)),
    SpecializationConcept("voice_acting", "Voice Acting", ("voice actor", "voice acting")),
    SpecializationConcept("voice_coaching", "Voice Coaching", ("voice coach", "voice coaching")),
    SpecializationConcept(
        "audio_engineering",
        "Audio Engineering",
        ("audio engineer", "audio engineering"),
    ),
)

CONCEPT_BY_KEY = {concept.key: concept for concept in CONCEPTS}
ALTERNATIVE_SEPARATOR = re.compile(r"^\s*(?:/|\bor\b)\s*$")


def specialization_requirements(title: str) -> list[dict]:
    """Return all-required groups; concepts within one group are alternatives."""
    text = _normalize_specialization_text(title)
    return [
        {"mode": mode, "concepts": list(concepts), "label": label}
        for mode, concepts, label in _specialization_requirements_cached(text)
    ]


@lru_cache(maxsize=16384)
def _specialization_requirements_cached(text: str) -> tuple[tuple[str, tuple[str, ...], str], ...]:
    mentions = _non_overlapping_mentions(text)
    if not mentions:
        return ()

    groups: list[list[str]] = []
    previous_end = None
    for start, end, key in mentions:
        separator = text[previous_end:start] if previous_end is not None else ""
        if groups and ALTERNATIVE_SEPARATOR.fullmatch(separator):
            if key not in groups[-1]:
                groups[-1].append(key)
        elif not groups or key not in groups[-1]:
            groups.append([key])
        previous_end = end

    return tuple(
        (
            "any_of" if len(keys) > 1 else "all_of",
            tuple(keys),
            _group_label(keys),
        )
        for keys in groups
    )


def specialization_evidence(profile: dict) -> set[str]:
    structured_values = [
        *(profile.get("skills") or []),
        *(profile.get("degrees_or_domains") or []),
    ]
    structured_text = _normalize_specialization_text(" ".join(str(value) for value in structured_values))
    raw_text = _normalize_specialization_text(profile.get("summary") or "")
    supported = set()
    for concept in CONCEPTS:
        raw_aliases = concept.raw_evidence_aliases or concept.aliases
        if any(_contains_alias(structured_text, alias) for alias in concept.aliases) or any(
            _contains_positive_alias(raw_text, alias) for alias in raw_aliases
        ):
            supported.add(concept.key)
            supported.update(concept.implies)
    return supported


def evaluate_specialization_requirements(
    title: str,
    profile: dict,
    supported_concepts: set[str] | None = None,
) -> dict:
    groups = specialization_requirements(title)
    evidence = supported_concepts if supported_concepts is not None else specialization_evidence(profile)
    missing = [group for group in groups if not evidence.intersection(group["concepts"])]
    supported = [
        {
            **group,
            "matched_concepts": sorted(evidence.intersection(group["concepts"])),
        }
        for group in groups
        if evidence.intersection(group["concepts"])
    ]
    return {
        "requirements": groups,
        "supported_groups": supported,
        "missing_groups": missing,
        "supported_concepts": sorted(evidence),
    }


@lru_cache(maxsize=16384)
def _non_overlapping_mentions(text: str) -> tuple[tuple[int, int, str], ...]:
    candidates = []
    for concept in CONCEPTS:
        for alias in concept.aliases:
            if _normalize_specialization_text(alias) not in text:
                continue
            for match in _alias_pattern(alias).finditer(text):
                candidates.append((match.start(), match.end(), concept.key))
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))

    selected = []
    for candidate in candidates:
        start, end, _ = candidate
        if any(start < existing_end and end > existing_start for existing_start, existing_end, _ in selected):
            continue
        selected.append(candidate)
    return tuple(sorted(selected))


def _contains_alias(text: str, alias: str) -> bool:
    return _alias_pattern(alias).search(text) is not None


def _contains_positive_alias(text: str, alias: str) -> bool:
    return any(
        not term_is_negated(text, match.start(), match.end())
        for match in _alias_pattern(alias).finditer(text)
    )


@lru_cache(maxsize=256)
def _alias_pattern(alias: str) -> re.Pattern:
    normalized = _normalize_specialization_text(alias)
    escaped = r"\s+".join(re.escape(part) for part in normalized.split())
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")


@lru_cache(maxsize=32768)
def _normalize_specialization_text(value: str | None) -> str:
    text = str(value or "").lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"[^a-z0-9#+/&().-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _group_label(keys: list[str]) -> str:
    labels = [CONCEPT_BY_KEY[key].label for key in keys]
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} or {labels[1]}"
    return ", ".join(labels[:-1]) + f", or {labels[-1]}"

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
import unicodedata


REQUIREMENT_NONE = "none"
REQUIREMENT_SINGLE = "single"
REQUIREMENT_ALL_REQUIRED = "all_required"
REQUIREMENT_ANY_SUPPORTED = "any_supported"
REQUIREMENT_AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class LanguageEligibility:
    detected_languages: frozenset[str]
    matched_languages: frozenset[str]
    unsupported_languages: frozenset[str]
    requirement_mode: str
    eligible_for_personalized: bool
    reason: str
    language_signal_allowed: bool


def normalize_language_text(value: str | None) -> str:
    text = str(value or "").lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


LANGUAGE_ALIASES = {
    "afrikaans": "afrikaans",
    "albanian": "albanian",
    "amharic": "amharic",
    "arabic": "arabic",
    "armenian": "armenian",
    "azerbaijani": "azerbaijani",
    "bengali": "bengali",
    "bangla": "bengali",
    "bosnian": "bosnian",
    "bulgarian": "bulgarian",
    "burmese": "burmese",
    "catalan": "catalan",
    "chinese": "chinese",
    "mandarin": "chinese",
    "cantonese": "chinese",
    "simplified chinese": "chinese",
    "traditional chinese": "chinese",
    "zh cn": "chinese",
    "zh tw": "chinese",
    "croatian": "croatian",
    "czech": "czech",
    "danish": "danish",
    "dutch": "dutch",
    "english": "english",
    "english us": "english",
    "english uk": "english",
    "english australia": "english",
    "english australian": "english",
    "english new zealand": "english",
    "en us": "english",
    "en uk": "english",
    "en gb": "english",
    "en au": "english",
    "en nz": "english",
    "estonian": "estonian",
    "farsi": "persian",
    "finnish": "finnish",
    "french": "french",
    "french france": "french",
    "french canada": "french",
    "francais": "french",
    "fr fr": "french",
    "fr ca": "french",
    "georgian": "georgian",
    "german": "german",
    "greek": "greek",
    "gujarati": "gujarati",
    "hebrew": "hebrew",
    "hindi": "hindi",
    "hungarian": "hungarian",
    "icelandic": "icelandic",
    "indonesian": "indonesian",
    "italian": "italian",
    "japanese": "japanese",
    "kannada": "kannada",
    "kazakh": "kazakh",
    "khmer": "khmer",
    "kinyarwanda": "kinyarwanda",
    "kiswahili": "kiswahili",
    "swahili": "kiswahili",
    "korean": "korean",
    "lao": "lao",
    "latvian": "latvian",
    "lithuanian": "lithuanian",
    "macedonian": "macedonian",
    "malay": "malay",
    "malayalam": "malayalam",
    "marathi": "marathi",
    "mongolian": "mongolian",
    "nepali": "nepali",
    "norwegian": "norwegian",
    "pashto": "pashto",
    "persian": "persian",
    "polish": "polish",
    "portuguese": "portuguese",
    "portuguese brazil": "portuguese",
    "portuguese brazilian": "portuguese",
    "portuguese portugal": "portuguese",
    "brazilian portuguese": "portuguese",
    "european portuguese": "portuguese",
    "pt br": "portuguese",
    "pt pt": "portuguese",
    "punjabi": "punjabi",
    "romanian": "romanian",
    "russian": "russian",
    "serbian": "serbian",
    "sinhala": "sinhala",
    "slovak": "slovak",
    "slovenian": "slovenian",
    "spanish": "spanish",
    "spanish spain": "spanish",
    "spanish mexico": "spanish",
    "spanish chile": "spanish",
    "spanish us": "spanish",
    "spanish united states": "spanish",
    "latin american spanish": "spanish",
    "espanol": "spanish",
    "castilian": "spanish",
    "es es": "spanish",
    "es mx": "spanish",
    "es us": "spanish",
    "es cl": "spanish",
    "swedish": "swedish",
    "tagalog": "tagalog",
    "filipino": "tagalog",
    "tamil": "tamil",
    "telugu": "telugu",
    "thai": "thai",
    "turkish": "turkish",
    "ukrainian": "ukrainian",
    "urdu": "urdu",
    "uzbek": "uzbek",
    "vietnamese": "vietnamese",
    "welsh": "welsh",
}

CANONICAL_LANGUAGES = frozenset(LANGUAGE_ALIASES.values())
_NORMALIZED_LANGUAGE_ALIASES = {
    normalize_language_text(alias): language
    for alias, language in LANGUAGE_ALIASES.items()
}
_ALIAS_PATTERNS = [
    (
        alias,
        canonical,
        re.compile(
            r"(?<![a-z0-9])"
            + r"\s+".join(re.escape(part) for part in alias.split())
            + r"(?![a-z0-9])"
        ),
    )
    for alias, canonical in sorted(
        _NORMALIZED_LANGUAGE_ALIASES.items(),
        key=lambda item: (-len(item[0]), item[0]),
    )
]


def language_eligibility(profile: dict, text: str) -> LanguageEligibility:
    profile_languages = profile_language_set(profile)
    return language_eligibility_for_languages(profile_languages, text)


def language_eligibility_for_languages(profile_languages: set[str], text: str) -> LanguageEligibility:
    mentions = find_language_mentions(text)
    detected = frozenset({mention["language"] for mention in mentions})
    mode = requirement_mode_for_mentions(text, mentions)
    matched = frozenset(detected & profile_languages)
    unsupported = frozenset(detected - profile_languages)

    if not detected:
        return LanguageEligibility(
            detected_languages=detected,
            matched_languages=matched,
            unsupported_languages=unsupported,
            requirement_mode=REQUIREMENT_NONE,
            eligible_for_personalized=True,
            reason="No explicit language requirement detected.",
            language_signal_allowed=False,
        )

    if mode in {REQUIREMENT_SINGLE, REQUIREMENT_ALL_REQUIRED}:
        eligible = not unsupported
        reason = (
            "Explicit language requirement matches profile."
            if eligible
            else "Explicit language requirement is not listed on profile."
        )
        return LanguageEligibility(
            detected_languages=detected,
            matched_languages=matched,
            unsupported_languages=unsupported,
            requirement_mode=mode,
            eligible_for_personalized=eligible,
            reason=reason,
            language_signal_allowed=eligible,
        )

    if mode == REQUIREMENT_ANY_SUPPORTED:
        eligible = bool(matched)
        reason = (
            "At least one listed alternative language matches profile."
            if eligible
            else "None of the listed alternative languages match profile."
        )
        return LanguageEligibility(
            detected_languages=detected,
            matched_languages=matched,
            unsupported_languages=unsupported,
            requirement_mode=mode,
            eligible_for_personalized=eligible,
            reason=reason,
            language_signal_allowed=eligible,
        )

    eligible = bool(matched)
    reason = (
        "Ambiguous multi-language opportunity shares a profile language."
        if eligible
        else "Ambiguous multi-language opportunity has no profile language match."
    )
    return LanguageEligibility(
        detected_languages=detected,
        matched_languages=matched,
        unsupported_languages=unsupported,
        requirement_mode=REQUIREMENT_AMBIGUOUS,
        eligible_for_personalized=eligible,
        reason=reason,
        language_signal_allowed=False,
    )


def find_language_mentions(text: str) -> list[dict]:
    normalized = normalize_language_text(text)
    return [dict(mention) for mention in find_language_mentions_cached(normalized)]


@lru_cache(maxsize=8192)
def find_language_mentions_cached(normalized: str) -> tuple[tuple[tuple[str, str | int], ...], ...]:
    mentions = []
    seen = set()
    for alias, language, pattern in _ALIAS_PATTERNS:
        for match in pattern.finditer(normalized):
            key = (match.start(), match.end(), language)
            if key in seen:
                continue
            seen.add(key)
            mentions.append(
                {
                    "language": language,
                    "alias": alias,
                    "start": match.start(),
                    "end": match.end(),
                }
            )
    mentions.sort(key=lambda item: (item["start"], -(item["end"] - item["start"])))
    return tuple(tuple(item.items()) for item in mentions)


def detect_explicit_languages(text: str) -> set[str]:
    return {mention["language"] for mention in find_language_mentions(text)}


def requirement_mode_for_mentions(text: str, mentions: list[dict]) -> str:
    languages = {mention["language"] for mention in mentions}
    if not languages:
        return REQUIREMENT_NONE
    if len(languages) == 1:
        return REQUIREMENT_SINGLE

    normalized = normalize_language_text(text)
    first = min(mention["start"] for mention in mentions)
    last = max(mention["end"] for mention in mentions)
    span = normalized[first:last]

    if re.search(r"\b(or|and or|either|one of|any of)\b", span):
        return REQUIREMENT_ANY_SUPPORTED

    if re.search(r"\b(to|into)\b", span) or re.search(r"\bfrom\b.*\bto\b", span):
        return REQUIREMENT_ALL_REQUIRED

    if "bilingual" in normalized or "language pair" in normalized:
        return REQUIREMENT_ALL_REQUIRED

    if len(languages) == 2 and re.search(
        r"\b(translation|translator|mtpe|post editing|localization)\b",
        normalized,
    ):
        return REQUIREMENT_ALL_REQUIRED

    if len(languages) == 2 and re.search(r"\band\b", span):
        return REQUIREMENT_ALL_REQUIRED

    return REQUIREMENT_AMBIGUOUS


def normalize_language_name(value: str | None) -> str:
    normalized = normalize_language_text(value)
    return _NORMALIZED_LANGUAGE_ALIASES.get(normalized, normalized)


def profile_language_set(profile: dict) -> set[str]:
    return {
        language
        for language in (
            normalize_language_name(value)
            for value in profile.get("languages", [])
        )
        if language
    }


def language_variants(language: str) -> list[str]:
    canonical = normalize_language_name(language)
    if not canonical:
        return []
    variants = sorted(
        alias
        for alias, language_name in _NORMALIZED_LANGUAGE_ALIASES.items()
        if language_name == canonical
    )
    return variants or [canonical]


def row_language_text(row: dict) -> str:
    values = [
        row_value(row, "title"),
        row_value(row, "canonical_title"),
        row_value(row, "department"),
        row_value(row, "expertise"),
        row_value(row, "source_category"),
        row_value(row, "commitment"),
    ]
    return " ".join(str(value or "") for value in values)


def row_value(row: dict, key: str):
    if hasattr(row, "get"):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError):
        return None

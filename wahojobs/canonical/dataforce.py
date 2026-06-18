import re
import unicodedata


LANGUAGE_PATTERNS = (
    r"\bTraditional Chinese\b(?:\s*\([^)]+\))?",
    r"\bSimplified Chinese\b(?:\s*\([^)]+\))?",
    r"\bChinese\b(?:\s*\([^)]+\))?",
    r"\bKorean\b(?:\s*\([^)]+\))?",
    r"\bJapanese\b(?:\s*\([^)]+\))?",
    r"\bPortuguese\b(?:\s*\([^)]+\))?",
    r"\bSpanish\b(?:\s*\([^)]+\))?",
    r"\bEnglish\b(?:\s*\([^)]+\))?",
    r"\bHindi\b(?:\s*\([^)]+\))?",
    r"\bArabic\b(?:\s*\([^)]+\))?",
    r"\bFrench\b(?:\s*\([^)]+\))?",
    r"\bGerman\b(?:\s*\([^)]+\))?",
    r"\bItalian\b(?:\s*\([^)]+\))?",
    r"\bTurkish\b(?:\s*\([^)]+\))?",
    r"\bThai\b(?:\s*\([^)]+\))?",
)
COUNTRY_NAMES = (
    "Australia",
    "Canada",
    "Denmark",
    "Finland",
    "France",
    "Greece",
    "India",
    "Ireland",
    "Italy",
    "Japan",
    "Kenya",
    "Mexico",
    "New Zealand",
    "Norway",
    "Poland",
    "Portugal",
    "South Africa",
    "Sweden",
    "Thailand",
    "Turkey",
    "United Kingdom",
    "United States",
)


def canonicalize_job(job):
    title = clean_title(job["title"])
    family_title = extract_family_title(title)
    language, language_locale = extract_language(title)
    source_category = normalize_source_category(
        job["expertise"] or job["department"] or "Unknown"
    )
    commitment = normalize_commitment(job["commitment"])

    canonical_key = "::".join(
        [
            "dataforce",
            normalize_key(family_title),
            normalize_key(source_category),
            normalize_key(language_locale or language or "none"),
            normalize_key(commitment or "none"),
        ]
    )

    return {
        "canonical_key": canonical_key,
        "canonical_title": build_canonical_title(family_title, language_locale or language, commitment),
        "normalized_title": normalize_title(family_title),
        "source_category": source_category,
        "language": language,
        "language_locale": language_locale,
    }


def extract_family_title(title):
    value = clean_title(title)
    value = re.sub(r"\s*\([^)]*(?:onsite|remote)[^)]*\)\s*$", "", value, flags=re.IGNORECASE)
    language, language_locale = extract_language(value)
    language_text = language_locale or language
    if language_text:
        value = re.sub(
            rf"\s*-\s*{re.escape(language_text)}\s*$",
            "",
            value,
            flags=re.IGNORECASE,
        )

    value = re.sub(
        rf"\s*-\s*({country_pattern()})\s*\((Minors)\)\s*$",
        r" (\2)",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        rf"\s*-\s*[^-]+\s+\(Project\s+([^)]+)\)\s*$",
        r" - Project \1",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\s*\(\s*Project\s+([^)]+)\)\s*$",
        r" - Project \1",
        value,
        flags=re.IGNORECASE,
    )

    suffix_patterns = (
        r"\s*-\s*(?:United States|US|USA)\s*-\s*[^-]+$",
        r"\s*-\s*(?:United States|US|USA)\s*\([^)]+\)$",
        rf"\s*-\s*(?:{country_pattern()})\s*-\s*[^-]+$",
        r"\s*-\s*[^-]+,\s*[^-]+$",
        r"\s*-\s*[^-]+\s+and\s+[^-]+$",
        r"\s*-\s*[^-]+(?:,\s*[^-]+)+$",
        rf"\s*-\s*(?:{country_pattern()})$",
    )
    for pattern in suffix_patterns:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE)

    value = re.sub(r"\s+", " ", value).strip(" -")
    return value or title or "Unknown"


def country_pattern():
    return "|".join(re.escape(country) for country in COUNTRY_NAMES)


def extract_language(title):
    for pattern in LANGUAGE_PATTERNS:
        match = re.search(pattern, title, flags=re.IGNORECASE)
        if not match:
            continue
        raw = normalize_spaces(match.group(0))
        locale_match = re.match(r"^(.+?)\s*\(([^)]+)\)$", raw)
        if locale_match:
            language = normalize_language_label(locale_match.group(1))
            locale = normalize_language_label(locale_match.group(2))
            return language, f"{language} ({locale})"
        return normalize_language_label(raw), None
    return None, None


def normalize_commitment(value):
    value = normalize_spaces(value)
    if not value:
        return None
    if value.lower() in {"remote", "on site"}:
        return value
    return value


def build_canonical_title(family_title, language, commitment):
    parts = [family_title]
    if language:
        parts.append(language)
    if commitment:
        parts.append(commitment)
    return " - ".join(parts)


def normalize_source_category(source_category):
    return normalize_spaces(source_category) or "Unknown"


def normalize_title(title):
    value = normalize_key(title).replace("-", " ")
    return value or "unknown"


def normalize_key(value):
    value = ascii_fold(value)
    value = value.lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unknown"


def normalize_language_label(value):
    return normalize_spaces(value)


def clean_title(value):
    return normalize_spaces(value)


def normalize_spaces(value):
    return re.sub(r"\s+", " ", (value or "").replace("–", "-").replace("—", "-")).strip()


def ascii_fold(value):
    value = unicodedata.normalize("NFKD", value or "")
    return value.encode("ascii", "ignore").decode("ascii")

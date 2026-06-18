import re
import unicodedata


LANGUAGES = (
    "Bahasa Indonesian",
    "Portuguese Brazil",
    "Portuguese Portugal",
    "Spanish Latam",
    "Spanish Spain",
    "English US",
    "English",
    "Spanish",
    "Portuguese",
    "German",
    "French",
    "Italian",
    "Korean",
    "Japanese",
    "Chinese",
    "Arabic",
    "Hindi",
    "Russian",
    "Dutch",
    "Tagalog",
    "Thai",
    "Malay",
    "Hebrew",
    "Finnish",
    "Turkish",
    "Swedish",
    "Danish",
)

SPECIALTY_RULES = (
    ("swe-bench", "swe_bench"),
    ("swe bench", "swe_bench"),
    ("gemini", "gemini"),
    ("python", "python"),
    ("javascript", "javascript"),
    ("java", "java"),
    ("ruby", "ruby"),
    ("rust", "rust"),
    ("c++", "cpp"),
    ("c#", "csharp"),
    (" go ", "go"),
    ("audio", "audio"),
    ("voice", "audio"),
    ("video", "video"),
    ("finance", "finance"),
    ("legal", "legal"),
    ("law", "legal"),
    ("clinical", "clinical"),
    ("healthcare", "healthcare"),
    ("medical", "healthcare"),
    ("annotation", "annotation"),
    ("multimodality", "multimodality"),
    ("repository validation", "repository_validation"),
    ("llm evaluation", "llm_evaluation"),
)

SENIORITY_RULES = (
    ("principal", "principal"),
    ("senior", "senior"),
    ("lead", "lead"),
    ("junior", "junior"),
)


def canonicalize_job(job):
    title = clean_title(job["title"])
    role_group = clean_title(job["expertise"] or job["department"] or "Unknown")
    language, language_locale = extract_language(title)
    specialty = extract_specialty(title, role_group)
    seniority = extract_seniority(title)
    role_family = normalize_title(strip_variant_terms(title))

    specialty_key = "-".join(value for value in (specialty, seniority) if value) or "none"
    canonical_key = "::".join(
        [
            "turing",
            normalize_key(role_family),
            normalize_key(role_group),
            normalize_key(language_locale or language or "none"),
            normalize_key(specialty_key),
        ]
    )

    return {
        "canonical_key": canonical_key,
        "canonical_title": build_canonical_title(
            strip_variant_terms(title),
            language_locale or language,
            specialty,
            seniority,
        ),
        "normalized_title": role_family,
        "source_category": role_group,
        "language": language,
        "language_locale": language_locale,
    }


def extract_language(title):
    normalized = normalize_spaces(title)
    locale_rules = (
        (r"\bSpanish\s*\(Latam\)", "Spanish", "Spanish Latin America"),
        (r"\bSpanish\s*\(Spain\)", "Spanish", "Spanish Spain"),
        (r"\bPortuguese\s*\(Brazil\)", "Portuguese", "Portuguese Brazil"),
        (r"\bPortuguese\s*\(Portugal\)", "Portuguese", "Portuguese Portugal"),
        (r"\bEnglish\s*\(US\)", "English", "English US"),
        (r"\bUS\)\s*Language\b", "English", "English US"),
    )
    for pattern, language, locale in locale_rules:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return language, locale

    for language in LANGUAGES:
        if re.search(rf"\b{re.escape(language)}\b", normalized, flags=re.IGNORECASE):
            base = language.split()[0]
            return base, language if language != base else None
    return None, None


def extract_specialty(title, role_group):
    haystack = f" {normalize_key(title).replace('-', ' ')} {normalize_key(role_group).replace('-', ' ')} "
    found = []
    for needle, label in SPECIALTY_RULES:
        normalized_needle = f" {normalize_key(needle).replace('-', ' ')} "
        if normalized_needle in haystack:
            found.append(label)
    return "-".join(dict.fromkeys(found)) if found else None


def extract_seniority(title):
    lowered = f" {title.lower()} "
    for needle, label in SENIORITY_RULES:
        if re.search(rf"\b{needle}\b", lowered):
            return label
    return None


def build_canonical_title(title, language, specialty, seniority):
    parts = [title]
    if language and not re.search(rf"\b{re.escape(language)}\b", title, flags=re.IGNORECASE):
        parts.append(language)
    if specialty:
        label = specialty.replace("_", " ").replace("-", " / ").title()
        if label.lower() not in title.lower():
            parts.append(label)
    if seniority and not re.search(rf"\b{re.escape(seniority)}\b", title, flags=re.IGNORECASE):
        parts.append(seniority.title())
    return " - ".join(parts)


def strip_variant_terms(title):
    value = clean_title(title)
    value = re.sub(r"\s*-\s*(US|USA|United States)[ -]?based\s*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*\((US|USA|United States)[ -]?based\)\s*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*\(US/Canada/WEU based\)\s*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" -")
    return value or title or "Unknown"


def normalize_title(title):
    return normalize_key(title).replace("-", " ") or "unknown"


def normalize_key(value):
    value = ascii_fold(value)
    value = value.lower()
    value = value.replace("&", " and ")
    value = value.replace("c++", "cpp")
    value = value.replace("c#", "csharp")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unknown"


def clean_title(value):
    return normalize_spaces(value)


def normalize_spaces(value):
    return re.sub(r"\s+", " ", (value or "").replace("â€“", "-").replace("â€”", "-")).strip()


def ascii_fold(value):
    value = unicodedata.normalize("NFKD", value or "")
    return value.encode("ascii", "ignore").decode("ascii")

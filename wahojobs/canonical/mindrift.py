import re
import unicodedata


LANGUAGES = (
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
)

SPECIALTY_RULES = (
    ("data scraping", "data_scraping"),
    ("web scraping", "web_scraping"),
    ("python", "python"),
    ("sql", "sql"),
    ("hackerrank", "hackerrank"),
    ("qa", "qa"),
    ("air", "air"),
    ("pytools", "pytools"),
    ("machine learning", "machine_learning"),
)


def canonicalize_job(job):
    title = clean_title(job["title"])
    department_family = extract_department_family(job["expertise"] or job["department"])
    language, language_locale = extract_language(title, job["expertise"] or job["department"])
    specialty = extract_specialty(title, job["expertise"] or job["department"], department_family)
    normalized_title = normalize_title(strip_generic_title_terms(title))

    canonical_key = "::".join(
        [
            "mindrift",
            normalize_key(normalized_title),
            normalize_key(department_family),
            normalize_key(language_locale or language or "none"),
            normalize_key(specialty or "none"),
        ]
    )

    return {
        "canonical_key": canonical_key,
        "canonical_title": build_canonical_title(
            strip_generic_title_terms(title),
            language_locale or language,
            specialty,
        ),
        "normalized_title": normalized_title,
        "source_category": department_family,
        "language": language,
        "language_locale": language_locale,
    }


def extract_department_family(department):
    parts = [part.strip() for part in (department or "").split(";") if part.strip()]
    parts = [part for part in parts if part != "Creator (Writer)"]
    if not parts:
        return "Unknown"

    preferred = [
        part
        for part in parts
        if not re.match(r"^English(?:[_ -].*)?$", part, flags=re.IGNORECASE)
    ]
    return preferred[-1] if preferred else parts[-1]


def extract_language(title, department):
    haystack = f"{title} {department or ''}"
    for language in LANGUAGES:
        match = re.search(rf"\b{re.escape(language)}\b", haystack, flags=re.IGNORECASE)
        if match:
            return language, None
    return None, None


def extract_specialty(title, department, department_family):
    haystack = normalize_spaces(f"{title} {department or ''} {department_family}")
    lowered = haystack.lower().replace("_", " ")
    found = []
    for needle, label in SPECIALTY_RULES:
        if re.search(rf"\b{re.escape(needle)}\b", lowered):
            found.append(label)
    return "-".join(dict.fromkeys(found)) if found else None


def build_canonical_title(title, language, specialty):
    parts = [title]
    if language and not re.search(rf"\b{re.escape(language)}\b", title, flags=re.IGNORECASE):
        parts.append(language)
    if specialty:
        label = specialty.replace("_", " ").replace("-", " / ").title()
        if label.lower() not in title.lower():
            parts.append(label)
    return " - ".join(parts)


def strip_generic_title_terms(title):
    value = clean_title(title)
    value = re.sub(r"\s*\([^)]*freelance[^)]*\)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*-\s*Freelance AI Trainer\s*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*-\s*AI Trainer\s*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*/\s*Freelance\s*", " / ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" -")
    return value or title or "Unknown"


def normalize_title(title):
    return normalize_key(title).replace("-", " ") or "unknown"


def normalize_key(value):
    value = ascii_fold(value)
    value = value.lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unknown"


def clean_title(value):
    return normalize_spaces(value)


def normalize_spaces(value):
    return re.sub(r"\s+", " ", (value or "").replace("–", "-").replace("—", "-")).strip()


def ascii_fold(value):
    value = unicodedata.normalize("NFKD", value or "")
    return value.encode("ascii", "ignore").decode("ascii")

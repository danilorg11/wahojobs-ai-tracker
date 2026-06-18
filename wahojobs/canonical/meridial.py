import re
import unicodedata


GENERIC_SUFFIX_PATTERNS = (
    r"\s*-\s*freelance ai trainer project$",
    r"\s*-\s*freelance project$",
    r"\s*-\s*ai trainer project\s*-\s*freelance$",
    r"\s*-\s*ai trainer project-\s*freelance$",
    r"\s*-\s*ai trainer project$",
)


def canonicalize_job(job):
    title = normalize_spaces(job["title"])
    source_category = normalize_source_category(
        job["expertise"] or job["department"] or "Unknown"
    )
    normalized_source_category = normalize_key(source_category)
    family_title = extract_role_family(title)
    language, language_locale = extract_language(title)
    specialty = extract_specialty(title)
    seniority = extract_seniority(title)

    canonical_key = "::".join(
        [
            "meridial",
            normalize_key(family_title),
            normalized_source_category,
            normalize_key(language_locale or language or "none"),
            normalize_key(specialty or "none"),
            normalize_key(seniority or "none"),
        ]
    )

    return {
        "canonical_key": canonical_key,
        "canonical_title": build_canonical_title(
            family_title,
            language_locale or language,
            specialty,
            seniority,
        ),
        "normalized_title": normalize_title(family_title),
        "source_category": source_category,
        "language": language,
        "language_locale": language_locale,
    }


def extract_role_family(title):
    value = strip_generic_suffixes(title)
    value = remove_generic_parentheticals(value)
    value = remove_fluent_phrase(value)
    value = re.sub(r"\b(Senior|Junior|Lead|Principal)\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" -")
    return value or "Unknown"


def strip_generic_suffixes(title):
    value = normalize_spaces(title)
    for pattern in GENERIC_SUFFIX_PATTERNS:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE)
    return value.strip(" -")


def remove_generic_parentheticals(title):
    def replace(match):
        text = normalize_spaces(match.group(1))
        if re.search(
            r"^(senior|junior|lead|principal|no prior experience needed|android device|us only)$",
            text,
            flags=re.IGNORECASE,
        ):
            return " "
        if re.search(r"^fluent in ", text, flags=re.IGNORECASE):
            return " "
        return f" {text} "

    return re.sub(r"\(([^)]*)\)", replace, title)


def remove_fluent_phrase(title):
    value = re.sub(r"\s*\(Fluent in [^)]+\)", " ", title, flags=re.IGNORECASE)
    value = re.sub(
        r"\bFluent in [A-Za-z ]+(?:\s*-\s*(?:Latin America|Spain|Portugal|Brazil))?",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    return value


def extract_language(title):
    value = normalize_spaces(title)

    patterns = [
        r"\(Fluent in ([^)]+)\)",
        r"\bFluent in ([A-Za-z ]+(?:\s*-\s*(?:Latin America|Spain|Portugal|Brazil))?)",
        r"^([A-Za-z]+(?:\s*\([^)]+\))?)\s+Language (?:Specialist|Expert)",
        r"^English\s+\(([^)]+)\)\s+Language",
        r"^Spanish\s+\(([^)]+)\)\s+Language",
        r"^Arabic\s+\(([^)]+)\)\s+Language",
        r"^French\s+\(([^)]+)\)\s+Language",
        r"^Chinese Language Specialist\s+\(([^)]+)\)",
    ]

    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            return clean_language(match.group(1))

    simple_match = re.match(
        r"^([A-Za-z]+)\s+Language (?:Specialist|Expert)",
        value,
        flags=re.IGNORECASE,
    )
    if simple_match:
        return normalize_language_label(simple_match.group(1)), None

    return None, None


def clean_language(value):
    value = normalize_spaces(value)
    if not value:
        return None, None

    value = value.strip(" -")
    if " - " in value:
        language, locale = [normalize_language_label(part) for part in value.split(" - ", 1)]
        return language, f"{language} {locale}"

    locale_match = re.match(r"^(.+?)\s*\(([^)]+)\)$", value)
    if locale_match:
        language = normalize_language_label(locale_match.group(1))
        locale = normalize_language_label(locale_match.group(2))
        return language, f"{language} {locale}"

    return normalize_language_label(value), None


def extract_specialty(title):
    value = normalize_spaces(title).lower()
    specialties = []
    if "android device" in value:
        specialties.append("android-device")
    if "us only" in value:
        specialties.append("us-only")
    if "latin america" in value:
        specialties.append("latin-america")

    family = normalize_key(extract_role_family(title))
    for technology in ("python", "kotlin", "latex"):
        if technology in family:
            specialties.append(technology)

    return "-".join(dict.fromkeys(specialties)) or None


def extract_seniority(title):
    value = normalize_spaces(title).lower()
    for seniority in ("principal", "senior", "lead", "junior"):
        if re.search(rf"\b{seniority}\b", value):
            return seniority
    return None


def build_canonical_title(family_title, language, specialty, seniority):
    parts = [family_title]
    if language:
        parts.append(language)
    if specialty:
        parts.append(specialty.replace("-", " ").title())
    if seniority:
        parts.append(seniority.title())
    return " - ".join(parts)


def normalize_source_category(source_category):
    return (source_category or "Unknown").strip() or "Unknown"


def normalize_title(title):
    return normalize_key(title).replace("-", " ") or "unknown"


def normalize_key(value):
    value = ascii_fold(value)
    value = value.lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unknown"


def normalize_language_label(value):
    value = normalize_spaces(value)
    replacements = {
        "LATAM": "Latin America",
        "LA": "Latin America",
    }
    return replacements.get(value, value)


def normalize_spaces(value):
    value = (value or "").replace("–", "-").replace("—", "-")
    value = value.replace("â€“", "-").replace("â€”", "-")
    return re.sub(r"\s+", " ", value).strip()


def ascii_fold(value):
    value = unicodedata.normalize("NFKD", value or "")
    return value.encode("ascii", "ignore").decode("ascii")

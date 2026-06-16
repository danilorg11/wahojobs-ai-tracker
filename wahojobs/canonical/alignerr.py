import re


LANGUAGE_NAMES = [
    "arabic",
    "bengali",
    "bulgarian",
    "chinese",
    "czech",
    "danish",
    "dutch",
    "english",
    "filipino",
    "french",
    "german",
    "greek",
    "gujarati",
    "hebrew",
    "hindi",
    "hungarian",
    "indonesian",
    "italian",
    "japanese",
    "kannada",
    "korean",
    "malay",
    "marathi",
    "norwegian",
    "polish",
    "portuguese",
    "romanian",
    "russian",
    "spanish",
    "swahili",
    "swedish",
    "tagalog",
    "tamil",
    "telugu",
    "thai",
    "turkish",
    "urdu",
    "vietnamese",
]

LOCALE_PATTERNS = [
    ("UK English", r"\buk english\b|\benglish\s*\((england|scotland|wales)\)"),
    ("US English", r"\bus english\b|\benglish\s*\(us\)"),
    ("Saudi Arabic", r"\bsaudi arabic\b"),
    ("Mexican Spanish", r"\bspanish\s*\(mexico\)|\bmexican spanish\b"),
]

GENERIC_PARENTHETICAL_WORDS = [
    "ai training",
    "remote",
    "contract",
    "freelance",
    "masters/phds",
    "master",
    "phd",
    "phds",
]


def canonicalize_job(title, source_category):
    normalized_title = normalize_title(title)
    source_category = normalize_source_category(source_category)
    language, language_locale = extract_language(title)
    language_key = normalize_key(language_locale or language or "none")
    canonical_key = "::".join(
        ["alignerr", normalized_title, normalize_key(source_category), language_key]
    )
    return {
        "canonical_key": canonical_key,
        "canonical_title": canonical_title(title),
        "normalized_title": normalized_title,
        "source_category": source_category,
        "language": language,
        "language_locale": language_locale,
    }


def normalize_title(title):
    value = normalize_text(title)
    value = re.sub(r"\(([^)]*)\)", replace_parenthetical, value)
    value = re.sub(r"\b(remote|contract|freelance)\b", " ", value)
    value = re.sub(r"\b(ai training|artificial intelligence training)\b", "ai training", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def replace_parenthetical(match):
    text = normalize_text(match.group(1))
    if any(word in text for word in GENERIC_PARENTHETICAL_WORDS):
        return " "
    return f" {text} "


def normalize_source_category(source_category):
    return (source_category or "Unknown").strip() or "Unknown"


def extract_language(title):
    normalized = normalize_text(title)
    for label, pattern in LOCALE_PATTERNS:
        if re.search(pattern, normalized):
            return label.split()[-1], label

    for language in LANGUAGE_NAMES:
        if re.search(rf"\b{re.escape(language)}\b", normalized):
            return language.title(), None

    return None, None


def canonical_title(title):
    return re.sub(r"\s+", " ", (title or "").strip())


def normalize_key(value):
    value = normalize_text(value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unknown"


def normalize_text(value):
    value = (value or "").lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[–—-]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()

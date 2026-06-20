import re
import unicodedata


WEAK_DOMAINS = {
    "generalist",
    "specialist",
    "expert",
    "other",
    "unknown",
}

LANGUAGE_ALIASES = {
    "australian english": ("English", "English Australia"),
    "singaporean english": ("English", "English Singapore"),
    "native english": ("English", None),
    "english": ("English", None),
    "spanish": ("Spanish", None),
    "french": ("French", None),
    "portuguese": ("Portuguese", None),
    "mandarin": ("Mandarin", None),
    "chinese": ("Chinese", None),
    "german": ("German", None),
    "japanese": ("Japanese", None),
    "korean": ("Korean", None),
    "arabic": ("Arabic", None),
    "bengali": ("Bengali", None),
    "bulgarian": ("Bulgarian", None),
    "czech": ("Czech", None),
    "dutch": ("Dutch", None),
    "hebrew": ("Hebrew", None),
    "hindi": ("Hindi", None),
    "indonesian": ("Indonesian", None),
    "italian": ("Italian", None),
    "javanese": ("Javanese", None),
    "kannada": ("Kannada", None),
    "kazakh": ("Kazakh", None),
    "khmer": ("Khmer", None),
    "kurdish": ("Kurdish", None),
    "malayalam": ("Malayalam", None),
    "marathi": ("Marathi", None),
    "norwegian": ("Norwegian", None),
    "nynorsk": ("Nynorsk", None),
    "odia": ("Odia", None),
    "romanian": ("Romanian", None),
    "sanskrit": ("Sanskrit", None),
    "swedish": ("Swedish", None),
    "swiss german": ("German", "German Swiss"),
    "tamil": ("Tamil", None),
    "tatar": ("Tatar", None),
    "telugu": ("Telugu", None),
    "thai": ("Thai", None),
    "turkish": ("Turkish", None),
    "welsh": ("Welsh", None),
    "wolof": ("Wolof", None),
}

LOCALE_ALIASES = {
    "canada": "Canada",
    "generalist": "Generalist",
    "mexico": "Mexico",
    "portugal": "Portugal",
    "spain": "Spain",
    "us or canada accent": "US or Canada Accent",
}

SPECIALTY_RULES = (
    ("corporate/m&a", "corporate-m-and-a"),
    ("corporate m&a", "corporate-m-and-a"),
    ("m&a", "m-and-a"),
    ("commercial real estate", "commercial-real-estate"),
    ("energy regulatory", "energy-regulatory"),
    ("energy compliance", "energy-compliance"),
    ("pii compliance", "pii-compliance"),
    ("qa automation", "qa-automation"),
    ("quality assurance", "quality-assurance"),
    ("machine learning", "machine-learning"),
    ("after effects", "after-effects"),
    ("eviews", "eviews"),
    ("qgis", "qgis"),
    ("python", "python"),
    ("javascript", "javascript"),
    ("typescript", "typescript"),
    ("java", "java"),
    ("c#", "csharp"),
    ("go", "go"),
    ("devops", "devops"),
    ("backend", "backend"),
    ("frontend", "frontend"),
    ("audio transcription", "audio-transcription"),
    ("audio recording", "audio-recording"),
    ("voice coach", "voice-coach"),
    ("voice over", "voice-over"),
    ("video qc", "video-qc"),
    ("video capture", "video-capture"),
    ("sensor data capture", "sensor-data-capture"),
    ("household data", "household-data"),
)

SENIORITY_RULES = ("principal", "senior", "lead", "junior")


def canonicalize_job(job):
    title = normalize_spaces(job["title"])
    domain_family = extract_domain_family(job["department"], title)
    language, language_locale = extract_language(title)
    specialty = extract_specialty(title)
    seniority = extract_seniority(title)
    commitment = normalize_commitment(job["commitment"])
    location_mode = normalize_location_mode(job["location"])
    role_family = extract_role_family(title)

    canonical_key = "::".join(
        [
            "micro1",
            normalize_key(role_family),
            normalize_key(domain_family),
            normalize_key(language_locale or language or "none"),
            normalize_key(specialty or "none"),
            normalize_key(seniority or "none"),
            normalize_key(commitment or "unknown"),
            normalize_key(location_mode or "remote"),
        ]
    )

    return {
        "canonical_key": canonical_key,
        "canonical_title": build_canonical_title(
            role_family,
            language_locale or language,
            specialty,
            seniority,
            commitment,
            location_mode,
        ),
        "normalized_title": normalize_title(role_family),
        "source_category": domain_family,
        "language": language,
        "language_locale": language_locale,
    }


def extract_domain_family(domain, title):
    domain = normalize_spaces(domain)
    normalized_domain = normalize_key(domain)
    if domain and normalized_domain not in WEAK_DOMAINS:
        return domain

    title_domain = infer_domain_from_title(title)
    return title_domain or domain or "Unknown"


def infer_domain_from_title(title):
    value = normalize_key(title).replace("-", " ")
    rules = (
        ("Language / Linguistics", ("language", "translation", "linguistic")),
        ("Audio / Speech", ("audio", "voice", "dubbing")),
        ("Coding / Software Evaluation", ("software", "developer", "engineer", "python", "java", "devops")),
        ("Finance", ("finance", "financial", "investment", "macroeconomic", "tax")),
        ("Legal", ("legal", "law", "attorney", "paralegal", "contracting")),
        ("Data Annotation", ("annotation", "quality analyst", "quality control")),
        ("Data Collection", ("data collection", "video capture", "sensor data", "household data")),
        ("AI / Machine Learning", ("machine learning", "ai engineer")),
        ("Technical Support / IT", ("technical support", "systems administrator", "network administration")),
    )
    for label, needles in rules:
        if any(needle in value for needle in needles):
            return label
    return None


def extract_role_family(title):
    value = normalize_spaces(title)
    value = remove_parenthetical_locale(value)
    value = re.sub(r"\b(Principal|Senior|Lead|Junior)\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" -")
    return value or "Unknown"


def remove_parenthetical_locale(title):
    def replace(match):
        text = normalize_spaces(match.group(1))
        if normalize_key(text).replace("-", " ") in LOCALE_ALIASES:
            return " "
        return f" {text} "

    return re.sub(r"\(([^)]*)\)", replace, title)


def extract_language(title):
    value = normalize_spaces(title)
    parenthetical_locale = extract_parenthetical_locale(value)

    for raw, (language, default_locale) in sorted(
        LANGUAGE_ALIASES.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        pattern = rf"\b{re.escape(raw)}\b"
        if not re.search(pattern, value, flags=re.IGNORECASE):
            continue
        locale = parenthetical_locale or default_locale
        return language, combine_language_locale(language, locale)

    return None, None


def extract_parenthetical_locale(title):
    matches = re.findall(r"\(([^)]*)\)", title)
    for text in matches:
        key = normalize_key(text).replace("-", " ")
        if key in LOCALE_ALIASES:
            return LOCALE_ALIASES[key]
    return None


def combine_language_locale(language, locale):
    if not locale:
        return None
    if locale.lower().startswith(language.lower()):
        return locale
    return f"{language} {locale}"


def extract_specialty(title):
    value = normalize_key(title).replace("-", " ")
    found = []
    for needle, label in SPECIALTY_RULES:
        normalized_needle = normalize_key(needle).replace("-", " ")
        if re.search(rf"\b{re.escape(normalized_needle)}\b", value):
            found.append(label)
    if "corporate-m-and-a" in found and "m-and-a" in found:
        found.remove("m-and-a")
    return "-".join(dict.fromkeys(found)) or None


def extract_seniority(title):
    for seniority in SENIORITY_RULES:
        if re.search(rf"\b{seniority}\b", title, flags=re.IGNORECASE):
            return seniority
    return None


def normalize_commitment(value):
    value = normalize_spaces(value)
    return value.lower() if value else None


def normalize_location_mode(value):
    value = normalize_spaces(value).lower()
    if not value or value == "remote":
        return None
    return value


def build_canonical_title(role_family, language, specialty, seniority, commitment, location_mode):
    parts = [role_family]
    if language and language.lower() not in role_family.lower():
        parts.append(language)
    if specialty:
        label = specialty.replace("-", " ").title()
        if normalize_title(label) not in normalize_title(role_family):
            parts.append(label)
    if seniority and not re.search(rf"\b{seniority}\b", role_family, flags=re.IGNORECASE):
        parts.append(seniority.title())
    if commitment:
        parts.append(commitment)
    if location_mode:
        parts.append(location_mode)
    return " - ".join(parts)


def normalize_title(title):
    return normalize_key(title).replace("-", " ") or "unknown"


def normalize_key(value):
    value = ascii_fold(value or "")
    value = value.lower()
    value = value.replace("&", " and ")
    value = value.replace("c++", "cpp")
    value = value.replace("c#", "csharp")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unknown"


def normalize_spaces(value):
    value = (value or "").replace("â€“", "-").replace("â€”", "-")
    value = value.replace("Ã¢â‚¬â€œ", "-").replace("Ã¢â‚¬â€", "-")
    return re.sub(r"\s+", " ", value).strip()


def ascii_fold(value):
    value = unicodedata.normalize("NFKD", value or "")
    return value.encode("ascii", "ignore").decode("ascii")

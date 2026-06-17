import re


def canonicalize_job(job):
    post_id = extract_post_id(job["external_id"])
    source_category = normalize_source_category(
        job["expertise"] or job["department"] or "Unknown"
    )
    canonical_title = extract_canonical_title(job["title"])

    return {
        "canonical_key": f"oneforma::{post_id}",
        "canonical_title": canonical_title,
        "normalized_title": normalize_title(canonical_title),
        "source_category": source_category,
        "language": None,
        "language_locale": None,
    }


def extract_post_id(external_id):
    parts = (external_id or "").split("::")
    if len(parts) >= 2 and parts[0] == "oneforma" and parts[1]:
        return parts[1]
    return "unknown"


def extract_canonical_title(title):
    title = re.sub(r"\s+", " ", (title or "").strip())
    if " - " in title and looks_like_language_or_locale(title.split(" - ", 1)[1]):
        return title.split(" - ", 1)[0].strip()
    return title


def looks_like_language_or_locale(value):
    value = (value or "").strip()
    return bool(
        re.search(r"\([^)]+\)", value)
        or re.search(r"\b[A-Za-z]+ - [A-Za-z]+", value)
        or re.search(r"\b(to|→)\b", value, flags=re.IGNORECASE)
    )


def normalize_source_category(source_category):
    return (source_category or "Unknown").strip() or "Unknown"


def normalize_title(title):
    value = (title or "").lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip() or "unknown"

import re
import unicodedata


def canonicalize_job(job):
    title = clean_title(job["title"])
    family = extract_family(title)
    language, language_locale = extract_language(title, family)
    specialty = extract_specialty(title, family)
    source_category = normalize_source_category(
        job["expertise"] or job["department"] or "Unknown"
    )

    canonical_key = "::".join(
        [
            "welocalize",
            normalize_key(family["key"]),
            normalize_key(language_locale or language or "none"),
            normalize_key(specialty or "none"),
        ]
    )

    return {
        "canonical_key": canonical_key,
        "canonical_title": build_canonical_title(family["title"], language_locale or language, specialty),
        "normalized_title": normalize_title(family["title"]),
        "source_category": source_category,
        "language": language,
        "language_locale": language_locale,
    }


def extract_family(title):
    normalized = normalize_spaces(title)

    rules = [
        (
            r"^Shape the Future of AI\s*[-—]\s*(.+?)\s+Talent Hub$",
            "shape_future_ai_talent_hub",
            "Shape the Future of AI Talent Hub",
        ),
        (
            r"^Shape the Future of AI\s*[-—]\s*(.+?)\s+Speakers in Israel$",
            "shape_future_ai_talent_hub",
            "Shape the Future of AI Talent Hub",
        ),
        (
            r"^Delta Crateris\s*-\s*AI Content Evaluator\s*-\s*(.+)$",
            "delta_crateris_ai_content_evaluator",
            "Delta Crateris AI Content Evaluator",
        ),
        (
            r"^Ara Zeta Project\s*-\s*AI Safety Evaluator\s*-\s*(.+)$",
            "ara_zeta_ai_safety_evaluator",
            "Ara Zeta AI Safety Evaluator",
        ),
        (
            r"^(?:REFFERAL\s+)?\*?Scout Search Quality Rater\s*-\s*(.+)$",
            "scout_search_quality_rater",
            "Scout Search Quality Rater",
        ),
        (
            r"^Remote Internet Search Quality Rater\s*-\s*(.+)$",
            "scout_search_quality_rater",
            "Scout Search Quality Rater",
        ),
        (
            r"^Ads Quality Rater\s*[-–]\s*(.+)$",
            "ads_quality_rater",
            "Ads Quality Rater",
        ),
        (
            r"^.*Advertisement Reviewer.*$",
            "ads_quality_rater",
            "Ads Quality Rater",
        ),
        (
            r"^.*Anzeigenpr.*$",
            "ads_quality_rater",
            "Ads Quality Rater",
        ),
        (
            r"^Czech AI Ads Reviewer$",
            "ads_quality_rater",
            "Ads Quality Rater",
        ),
        (
            r"^Project Lynx\s*-\s*Audio Recording Director\s*-\s*(.+)$",
            "project_lynx_audio_recording_director",
            "Project Lynx Audio Recording Director",
        ),
        (
            r"^Project Lynx\s*-\s*Studio Recording Speaker\s*-\s*(.+)$",
            "project_lynx_studio_recording_speaker",
            "Project Lynx Studio Recording Speaker",
        ),
        (
            r"^Project Lynx\s*-\s*Quality Reviewer \(Spoken Content\)\s*-\s*(.+)$",
            "project_lynx_quality_reviewer_spoken_content",
            "Project Lynx Quality Reviewer Spoken Content",
        ),
        (
            r"^Project Lynx\s*-\s*Audio Recording Transcriber\s*-\s*(.+)$",
            "project_lynx_audio_recording_transcriber",
            "Project Lynx Audio Recording Transcriber",
        ),
        (
            r"^Translation Validator\s*\|\s*(.+)$",
            "translation_validator",
            "Translation Validator",
        ),
        (
            r"^Alpheratz Project\s*-\s*(.+)\s+Translation Quality Reviewer$",
            "alpheratz_translation_quality_reviewer",
            "Alpheratz Translation Quality Reviewer",
        ),
        (
            r"^Alpheratz Project\s*-\s*(.+)\s+Translation Quality Rater$",
            "alpheratz_translation_quality_rater",
            "Alpheratz Translation Quality Rater",
        ),
        (
            r"^Project Lion\s*-\s*(?:Lead |Senior )?Prompt Engineer\s*-\s*(.+?)\s*\(Remote, Part-Time\)$",
            "project_lion_prompt_engineer",
            "Project Lion Prompt Engineer",
        ),
        (
            r"^Project Perseus\s*\|\s*Data Labeling Associate\s*-\s*(.+?)\s+Speakers.*$",
            "project_perseus_data_labeling_associate",
            "Project Perseus Data Labeling Associate",
        ),
        (
            r"^Project Perseus\s*\|\s*Data Quality Analyst\s*-\s*(.+?)\s+Speakers.*$",
            "project_perseus_data_quality_analyst",
            "Project Perseus Data Quality Analyst",
        ),
        (
            r"^Apus\s*-\s*Audio Transcription Analyst\s+(.+)$",
            "apus_audio_transcription_analyst",
            "Apus Audio Transcription Analyst",
        ),
        (
            r"^Apus:\s*Audio Transcription Verifier\s+(.+)$",
            "apus_audio_transcription_verifier",
            "Apus Audio Transcription Verifier",
        ),
        (
            r"^Pegasus\s*-\s*(.+)\s+Audio Evaluator$",
            "pegasus_audio_evaluator",
            "Pegasus Audio Evaluator",
        ),
        (
            r"^Maps Personalization Relevance Rater\s*-\s*(.+)$",
            "maps_personalization_relevance_rater",
            "Maps Personalization Relevance Rater",
        ),
        (
            r"^Bharani-\s*Prompt Creation Specialist-\s*(.+)$",
            "bharani_prompt_creation_specialist",
            "Bharani Prompt Creation Specialist",
        ),
        (
            r"^Hamal-\s*Prompt Creation Expert\s+(.+)$",
            "hamal_prompt_creation_expert",
            "Hamal Prompt Creation Expert",
        ),
        (
            r"^Generative AI Analyst(?:\s*\|\s*|\s*\()(.+?)(?:\))?$",
            "generative_ai_analyst",
            "Generative AI Analyst",
        ),
        (
            r"^Red Teaming\s*\|\s*Generative AI Analyst\s*-\s*(.+)$",
            "red_teaming_generative_ai_analyst",
            "Red Teaming Generative AI Analyst",
        ),
        (
            r"^Entry-Level AI Data Rater\s*-\s*(.+)$",
            "entry_level_ai_data_rater",
            "Entry-Level AI Data Rater",
        ),
        (
            r"^Audio Recording Project\s*-\s*(.+)$",
            "audio_recording_project",
            "Audio Recording Project",
        ),
        (
            r"^(.+)\s+Bilingual Audio Specialist$",
            "bilingual_audio_specialist",
            "Bilingual Audio Specialist",
        ),
    ]

    for pattern, key, title in rules:
        if re.match(pattern, normalized, flags=re.IGNORECASE):
            return {"key": key, "title": title}

    return {
        "key": strip_variant_suffix(normalized),
        "title": strip_variant_suffix(normalized),
    }


def extract_language(title, family):
    normalized = normalize_spaces(title)
    family_key = family["key"]

    if family_key == "ads_quality_rater":
        if re.match(r"^Czech AI Ads Reviewer$", normalized, flags=re.IGNORECASE):
            return "Czech", None
        if re.search(r"Anzeigenpr", normalized, flags=re.IGNORECASE):
            return "German", None
        if "Advertisement Reviewer" in normalized:
            match = re.match(r"^(.+?)\s+Advertisement Reviewer", normalized)
            return clean_language(match.group(1) if match else None)

    extraction_patterns = [
        r"^Shape the Future of AI\s*[-—]\s*(.+?)\s+(?:Talent Hub|Speakers in Israel)$",
        r"^Delta Crateris\s*-\s*AI Content Evaluator\s*-\s*(.+)$",
        r"^Ara Zeta Project\s*-\s*AI Safety Evaluator\s*-\s*(.+)$",
        r"^(?:REFFERAL\s+)?\*?Scout Search Quality Rater\s*-\s*(.+)$",
        r"^Remote Internet Search Quality Rater\s*-\s*(.+)$",
        r"^Ads Quality Rater\s*[-–]\s*(.+)$",
        r"^Project Lynx\s*-\s*Audio Recording Director\s*-\s*(.+)$",
        r"^Project Lynx\s*-\s*Studio Recording Speaker\s*-\s*(.+)$",
        r"^Project Lynx\s*-\s*Quality Reviewer \(Spoken Content\)\s*-\s*(.+)$",
        r"^Project Lynx\s*-\s*Audio Recording Transcriber\s*-\s*(.+)$",
        r"^Translation Validator\s*\|\s*(.+)$",
        r"^Alpheratz Project\s*-\s*(.+)\s+Translation Quality (?:Reviewer|Rater)$",
        r"^Project Perseus\s*\|\s*Data (?:Labeling Associate|Quality Analyst)\s*-\s*(.+?)\s+Speakers.*$",
        r"^Apus\s*-\s*Audio Transcription Analyst\s+(.+)$",
        r"^Apus:\s*Audio Transcription Verifier\s+(.+)$",
        r"^Pegasus\s*-\s*(.+)\s+Audio Evaluator$",
        r"^Maps Personalization Relevance Rater\s*-\s*(.+)$",
        r"^Bharani-\s*Prompt Creation Specialist-\s*(.+)$",
        r"^Hamal-\s*Prompt Creation Expert\s+(.+)$",
        r"^Generative AI Analyst(?:\s*\|\s*|\s*\()(.+?)(?:\))?$",
        r"^Entry-Level AI Data Rater\s*-\s*(.+)$",
        r"^Audio Recording Project\s*-\s*(.+)$",
        r"^(.+)\s+Bilingual Audio Specialist$",
    ]

    for pattern in extraction_patterns:
        match = re.match(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return clean_language(match.group(1))

    return None, None


def clean_language(value):
    value = normalize_spaces(value)
    if not value:
        return None, None

    value = re.sub(r"\s*\|\s*.*$", "", value)
    value = re.sub(r"\s*-\s*Onsite.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*-\s*USA.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+Speakers.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*\(Remote, Part-Time\)$", "", value, flags=re.IGNORECASE)
    value = value.strip(" -|")

    locale_match = re.search(r"^(.+?)\s*\(([^)]+)\)$", value)
    if locale_match:
        language = normalize_language_label(locale_match.group(1))
        locale = normalize_language_label(locale_match.group(2))
        if should_keep_locale(language, locale):
            return language, f"{language} ({locale})"
        return language, None

    return normalize_language_label(value), None


def should_keep_locale(language, locale):
    language_key = normalize_key(language)
    locale_key = normalize_key(locale)
    if language_key in {"chinese-zh-cn", "zh-cn", "chinese-simplified"}:
        return True
    if "taiwanese" in locale_key or "traditional" in locale_key:
        return True
    if locale_key in {"zh-cn", "zh-tw"}:
        return True
    return False


def extract_specialty(title, family):
    title_lower = normalize_spaces(title).lower()

    if "junior" in title_lower:
        return "junior"
    if "senior prompt engineer" in title_lower:
        return "senior_prompt_engineer"
    if "lead prompt engineer" in title_lower:
        return "lead_prompt_engineer"
    if "prompt engineer" in title_lower and family["key"] == "project_lion_prompt_engineer":
        return "prompt_engineer"
    return None


def build_canonical_title(family_title, language, specialty):
    parts = [family_title]
    if language:
        parts.append(language)
    if specialty:
        parts.append(specialty.replace("_", " ").title())
    return " - ".join(parts)


def strip_variant_suffix(title):
    value = re.sub(r"\s*\([^)]*\)", "", title)
    value = re.sub(r"\s*\|\s*.*$", "", value)
    value = re.sub(r"\s+-\s+[^-]+$", "", value)
    return normalize_spaces(value) or "Unknown"


def normalize_source_category(source_category):
    return (source_category or "Unknown").strip() or "Unknown"


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
    value = normalize_spaces(value)
    fixes = {
        "zh-CN": "Chinese zh-CN",
        "Unites States": "United States",
    }
    return fixes.get(value, value)


def clean_title(value):
    return normalize_spaces((value or "").lstrip("*"))


def normalize_spaces(value):
    value = (value or "").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", value).strip()


def ascii_fold(value):
    value = unicodedata.normalize("NFKD", value or "")
    return value.encode("ascii", "ignore").decode("ascii")

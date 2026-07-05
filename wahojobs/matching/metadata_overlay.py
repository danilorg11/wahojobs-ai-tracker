from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OVERLAY_PATH = ROOT / "exports" / "opportunity_metadata_inferred_overlay.json"


@dataclass(frozen=True)
class OpportunityMetadataOverlay:
    path: Path
    records_by_key: dict[str, dict]

    @property
    def enabled(self) -> bool:
        return bool(self.records_by_key)


def load_overlay(path: Path | None = None, required: bool = False) -> OpportunityMetadataOverlay:
    overlay_path = path or DEFAULT_OVERLAY_PATH
    if not overlay_path.exists():
        if required:
            raise FileNotFoundError(f"Opportunity metadata overlay not found: {overlay_path}")
        return OpportunityMetadataOverlay(path=overlay_path, records_by_key={})

    data = json.loads(overlay_path.read_text(encoding="utf-8"))
    validate_overlay_payload(data, overlay_path)
    return OpportunityMetadataOverlay(
        path=overlay_path,
        records_by_key={
            str(record["stable_opportunity_key"]): record
            for record in data["records"]
        },
    )


def validate_overlay_payload(data: dict, path: Path) -> None:
    if not isinstance(data, dict):
        raise ValueError(f"Overlay must be a JSON object: {path}")
    if data.get("schema_version") != 1:
        raise ValueError(f"Unsupported overlay schema_version in {path}: {data.get('schema_version')!r}")
    records = data.get("records")
    if not isinstance(records, list):
        raise ValueError(f"Overlay records must be a list: {path}")

    seen = set()
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"Overlay record #{index} must be an object.")
        key = record.get("stable_opportunity_key")
        if not key:
            raise ValueError(f"Overlay record #{index} is missing stable_opportunity_key.")
        if key in seen:
            raise ValueError(f"Duplicate overlay stable_opportunity_key: {key}")
        seen.add(key)
        provenance = record.get("provenance")
        if not isinstance(provenance, list) or not provenance:
            raise ValueError(f"Overlay record {key} is missing provenance.")
        for field in ("required_languages", "language_locale", "location_restriction"):
            values = record.get(field)
            if not isinstance(values, list):
                raise ValueError(f"Overlay record {key} field {field} must be a list.")


def apply_overlay_to_rows(rows, overlay: OpportunityMetadataOverlay | None) -> list[dict]:
    if overlay is None or not overlay.enabled:
        return [dict(row) for row in rows]
    return [apply_overlay_to_row(row, overlay) for row in rows]


def apply_overlay_to_row(row, overlay: OpportunityMetadataOverlay | None) -> dict:
    enriched = dict(row)
    if overlay is None or not overlay.enabled:
        return enriched

    key, record = find_overlay_record(enriched, overlay)
    if not record:
        return enriched

    required_languages = clean_list(record.get("required_languages"))
    language_locale = clean_list(record.get("language_locale"))
    location_restriction = clean_list(record.get("location_restriction"))

    if required_languages:
        enriched["required_languages"] = language_requirement_text(required_languages)
    if language_locale and not clean(enriched.get("language_locale")):
        enriched["language_locale"] = "; ".join(language_locale)

    enriched["metadata_overlay_applied"] = True
    enriched["metadata_overlay_key"] = key
    enriched["metadata_overlay_source"] = record.get("metadata_source") or "human_reviewed_title_inference"
    enriched["overlay_required_languages"] = required_languages
    enriched["overlay_language_locale"] = language_locale
    enriched["overlay_location_restriction"] = location_restriction
    enriched["overlay_review_ids"] = [
        clean(provenance.get("review_id"))
        for provenance in record.get("provenance", [])
        if clean(provenance.get("review_id"))
    ]
    enriched["overlay_provenance"] = list(record.get("provenance") or [])
    enriched["overlay_warnings"] = clean_list(record.get("warnings"))
    return enriched


def find_overlay_record(row: dict, overlay: OpportunityMetadataOverlay) -> tuple[str, dict | None]:
    for key in stable_keys_for_row(row):
        record = overlay.records_by_key.get(key)
        if record:
            return key, record
    return "", None


def stable_keys_for_row(row: dict) -> list[str]:
    source = clean(row.get("source_slug") or row.get("source"))
    keys = []
    job_id = clean(row.get("job_id"))
    if job_id:
        keys.append(f"job_id:{source}:{job_id}")
    external_id = clean(row.get("external_id"))
    if external_id:
        keys.append(f"external_id:{source}:{external_id}")
    source_hash = clean(row.get("source_hash"))
    if source_hash:
        keys.append(f"source_hash:{source}:{source_hash}")
    url = clean(row.get("url"))
    title = clean(row.get("title"))
    if source and (url or title):
        keys.append(f"source_url_title:{source}:{slug(url)}:{slug(title)}")
    canonical_id = clean(row.get("canonical_opportunity_id"))
    if canonical_id:
        keys.append(f"canonical_opportunity_id:{canonical_id}")
    return keys


def language_requirement_text(languages: list[str]) -> str:
    if len(languages) <= 1:
        return languages[0] if languages else ""
    return " and ".join(languages)


def clean_list(values) -> list[str]:
    if not isinstance(values, list):
        return []
    result = []
    seen = set()
    for value in values:
        text = clean(value)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def slug(value: str) -> str:
    value = clean(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unknown"


def clean(value) -> str:
    return str(value or "").strip()

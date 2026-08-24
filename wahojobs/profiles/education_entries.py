"""Closed structured education entries and deterministic legacy projection."""

from __future__ import annotations

from copy import deepcopy
import re
import unicodedata

from wahojobs.profiles.canonical import (
    EDUCATION_COMPLETION_STATUSES,
    EDUCATION_LEVELS,
    UNKNOWN,
)


MAX_EDUCATION_ENTRIES = 24
MAX_EDUCATION_ENTRY_TEXT_CHARS = 128
EDUCATION_ENTRY_FIELDS = frozenset(
    {
        "kind",
        "qualification",
        "field",
        "institution",
        "status",
        "completion_year",
    }
)
EDUCATION_ENTRY_KINDS = frozenset(EDUCATION_LEVELS - {"no_degree"})
EDUCATION_ENTRY_STATUSES = frozenset(EDUCATION_COMPLETION_STATUSES)
LEGACY_EDUCATION_FIELDS = frozenset(
    {
        "education_level",
        "degrees",
        "fields_or_domains",
        "institutions",
        "graduation_years",
        "completion_status",
    }
)


class EducationEntryContractError(ValueError):
    """Bounded value-free rejection for education entry contract failures."""

    def __init__(self, *reason_codes):
        self.reason_codes = tuple(sorted(set(reason_codes or ("invalid_entries",))))[:16]
        super().__init__(
            "education entries rejected; reason_codes="
            + ",".join(self.reason_codes)
        )


def canonicalize_education_entries_v1(value):
    """Validate and return one deterministically ordered education entry list."""

    if type(value) is not list or len(value) > MAX_EDUCATION_ENTRIES:
        raise EducationEntryContractError("invalid_entry_list")
    normalized = [_canonical_entry(item) for item in value]
    identities = [_entry_identity(item) for item in normalized]
    if len(identities) != len(set(identities)):
        raise EducationEntryContractError("duplicate_entry")
    normalized.sort(key=_entry_sort_key)
    return deepcopy(normalized)


def project_education_entries_to_legacy(entries, unpaired_legacy=None):
    """Build exact Canonical V1 shadows from entries plus unpaired legacy facts.

    ``unpaired_legacy`` is server-produced and may contain only fields whose
    source relationship was not strong enough to place them in an entry.
    """

    canonical_entries = canonicalize_education_entries_v1(entries)
    unpaired = _canonical_unpaired_legacy(unpaired_legacy or {})

    degrees = _unique_labels(
        [*unpaired.get("degrees", []), *(
            item["qualification"] for item in canonical_entries
            if item["qualification"]
        )]
    )
    fields = _unique_labels(
        [*unpaired.get("fields_or_domains", []), *(
            item["field"] for item in canonical_entries if item["field"]
        )]
    )
    institutions = _unique_labels(
        [*unpaired.get("institutions", []), *(
            item["institution"] for item in canonical_entries
            if item["institution"]
        )]
    )
    years = sorted(
        set(
            [*unpaired.get("graduation_years", []), *(
                item["completion_year"] for item in canonical_entries
                if item["completion_year"] is not None
            )]
        )
    )

    kinds = [item["kind"] for item in canonical_entries]
    if "education_level" in unpaired:
        kinds.append(unpaired["education_level"])
    asserted_kinds = {item for item in kinds if item != "not_specified"}
    education_level = (
        next(iter(asserted_kinds))
        if len(asserted_kinds) == 1
        else "not_specified"
    )

    statuses = [item["status"] for item in canonical_entries]
    if "completion_status" in unpaired:
        statuses.append(unpaired["completion_status"])
    distinct_statuses = set(statuses)
    completion_status = (
        next(iter(distinct_statuses))
        if len(distinct_statuses) == 1
        else UNKNOWN
    )

    return {
        "education_level": education_level,
        "degrees": degrees,
        "fields_or_domains": fields,
        "institutions": institutions,
        "graduation_years": years,
        "completion_status": completion_status,
    }


def education_entry_identity(value):
    canonical = canonicalize_education_entries_v1([value])
    return _entry_identity(canonical[0])


def _canonical_entry(value):
    if type(value) is not dict or set(value) != EDUCATION_ENTRY_FIELDS:
        raise EducationEntryContractError("invalid_entry_fields")
    kind = value["kind"]
    status = value["status"]
    if kind not in EDUCATION_ENTRY_KINDS:
        raise EducationEntryContractError("invalid_entry_kind")
    if status not in EDUCATION_ENTRY_STATUSES:
        raise EducationEntryContractError("invalid_entry_status")
    qualification = _text(value["qualification"])
    field = _text(value["field"])
    institution = _text(value["institution"])
    year = value["completion_year"]
    if year is not None and (
        type(year) is not int or not 1900 <= year <= 2200
    ):
        raise EducationEntryContractError("invalid_completion_year")
    if (
        kind == "not_specified"
        and not qualification
        and not field
        and not institution
    ):
        raise EducationEntryContractError("empty_entry")
    return {
        "kind": kind,
        "qualification": qualification,
        "field": field,
        "institution": institution,
        "status": status,
        "completion_year": year,
    }


def _canonical_unpaired_legacy(value):
    if type(value) is not dict or set(value) - LEGACY_EDUCATION_FIELDS:
        raise EducationEntryContractError("invalid_legacy_summary")
    result = {}
    for name in ("degrees", "fields_or_domains", "institutions"):
        if name in value:
            if type(value[name]) is not list:
                raise EducationEntryContractError("invalid_legacy_summary")
            result[name] = _unique_labels(value[name])
    if "graduation_years" in value:
        years = value["graduation_years"]
        if type(years) is not list or any(
            type(year) is not int or not 1900 <= year <= 2200
            for year in years
        ):
            raise EducationEntryContractError("invalid_legacy_summary")
        result["graduation_years"] = sorted(set(years))
    if "education_level" in value:
        if value["education_level"] not in EDUCATION_LEVELS:
            raise EducationEntryContractError("invalid_legacy_summary")
        result["education_level"] = value["education_level"]
    if "completion_status" in value:
        if value["completion_status"] not in EDUCATION_COMPLETION_STATUSES:
            raise EducationEntryContractError("invalid_legacy_summary")
        result["completion_status"] = value["completion_status"]
    return result


def _text(value):
    if type(value) is not str or "\x00" in value:
        raise EducationEntryContractError("invalid_entry_text")
    normalized = " ".join(unicodedata.normalize("NFC", value).split())
    if len(normalized) > MAX_EDUCATION_ENTRY_TEXT_CHARS or any(
        ord(char) < 32 or 127 <= ord(char) <= 159
        for char in normalized
    ):
        raise EducationEntryContractError("invalid_entry_text")
    return normalized


def _unique_labels(values):
    if type(values) not in {list, tuple}:
        raise EducationEntryContractError("invalid_legacy_summary")
    by_identity = {}
    for value in values:
        normalized = _text(value)
        if not normalized:
            raise EducationEntryContractError("invalid_legacy_summary")
        by_identity.setdefault(_comparison(normalized), normalized)
    return sorted(
        by_identity.values(),
        key=lambda item: (_comparison(item), item),
    )


def _comparison(value):
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _entry_identity(item):
    return (
        item["kind"],
        _comparison(item["qualification"]),
        _comparison(item["field"]),
        _comparison(item["institution"]),
        item["status"],
        item["completion_year"],
    )


def _entry_sort_key(item):
    return (
        item["completion_year"] is None,
        item["completion_year"] or 0,
        _comparison(item["institution"]),
        _comparison(item["qualification"]),
        _comparison(item["field"]),
        item["kind"],
        item["status"],
    )


__all__ = (
    "EDUCATION_ENTRY_FIELDS",
    "EDUCATION_ENTRY_KINDS",
    "EDUCATION_ENTRY_STATUSES",
    "EducationEntryContractError",
    "MAX_EDUCATION_ENTRIES",
    "MAX_EDUCATION_ENTRY_TEXT_CHARS",
    "canonicalize_education_entries_v1",
    "education_entry_identity",
    "project_education_entries_to_legacy",
)

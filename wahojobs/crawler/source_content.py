"""Small helpers for forwarding public provider content without filling gaps."""

from __future__ import annotations


def first_text(record: dict, fields) -> str | None:
    """Return the first provider-supplied, non-empty string without rewriting it."""

    for field in fields:
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return None


def selected_metadata(record: dict, fields) -> dict:
    """Copy only explicitly present public fields, preserving nulls and structure."""

    return {field: record[field] for field in fields if field in record}


def nonempty_metadata(value: dict) -> dict | None:
    return value or None

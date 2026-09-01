"""Closed evidence-origin authority shared by opportunity fact consumers."""

from __future__ import annotations


OBJECTIVE_EXCLUSION_AUTHORIZED_EVIDENCE_BASES = frozenset(
    {"deterministic_parse", "source_explicit"}
)


def evidence_record_has_objective_exclusion_authority(record) -> bool:
    """Return whether one evidence record may support candidate exclusion."""

    return (
        type(record) is dict
        and record.get("basis") in OBJECTIVE_EXCLUSION_AUTHORIZED_EVIDENCE_BASES
        and record.get("confidence") == "high"
    )

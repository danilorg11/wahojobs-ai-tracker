"""Network-free adapter boundary for future profile extraction models."""

from __future__ import annotations

from typing import Protocol

from wahojobs.profile_intake.contracts import (
    AIProfileExtraction,
    ModelEvidencePacket,
    ProfileIntakeError,
    _bounded_json_copy,
    validate_ai_profile_extraction,
)
from wahojobs.profile_intake.minimization import require_minimized_model_evidence


class ProfileExtractionAdapter(Protocol):
    """Accept only bounded evidence and return a validated ephemeral contract."""

    def extract(self, evidence: ModelEvidencePacket) -> AIProfileExtraction:
        """Extract supported profile facts from one bounded evidence packet."""


class DeterministicFakeProfileExtractionAdapter:
    """Return one configured response through the production validator.

    This class imports no HTTP or model client and performs no I/O.  It is for
    deterministic tests and for later orchestration tests before a real model
    adapter exists.
    """

    def __init__(self, response: object):
        self._response = _bounded_json_copy(response)

    def extract(self, evidence: ModelEvidencePacket) -> AIProfileExtraction:
        if type(evidence) is not ModelEvidencePacket:
            raise ProfileIntakeError("invalid_model_evidence_packet")
        require_minimized_model_evidence(evidence)
        response = _bounded_json_copy(self._response)
        return validate_ai_profile_extraction(response, evidence)

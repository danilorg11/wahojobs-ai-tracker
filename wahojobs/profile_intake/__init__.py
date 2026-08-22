"""Security-bounded, ephemeral foundations for AI-assisted profile intake."""

from wahojobs.profile_intake.adapter import (
    DeterministicFakeProfileExtractionAdapter,
    ProfileExtractionAdapter,
)
from wahojobs.profile_intake.contracts import (
    AI_EXTRACTION_SCHEMA_VERSION,
    DEFAULT_DOCUMENT_LIMITS,
    AIProfileExtraction,
    DocumentFormat,
    DocumentKind,
    DocumentLimits,
    EvidenceBlock,
    EvidencePacket,
    ExtractedDocument,
    ExtractedFact,
    ParserMetadata,
    ProfileIntakeError,
    new_document_reference,
    validate_ai_profile_extraction,
)
from wahojobs.profile_intake.documents import extract_resume_document

__all__ = (
    "AI_EXTRACTION_SCHEMA_VERSION",
    "DEFAULT_DOCUMENT_LIMITS",
    "AIProfileExtraction",
    "DeterministicFakeProfileExtractionAdapter",
    "DocumentFormat",
    "DocumentKind",
    "DocumentLimits",
    "EvidenceBlock",
    "EvidencePacket",
    "ExtractedDocument",
    "ExtractedFact",
    "ParserMetadata",
    "ProfileExtractionAdapter",
    "ProfileIntakeError",
    "extract_resume_document",
    "new_document_reference",
    "validate_ai_profile_extraction",
)

"""Deterministic PII minimization before profile evidence reaches a model."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from wahojobs.profile_intake.contracts import (
    EvidencePacket,
    ModelEvidenceBlock,
    ModelEvidencePacket,
    ProfileIntakeError,
)


MODEL_EVIDENCE_SCHEMA_VERSION = "model_evidence_packet_v1"

_EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+(?![\w.-])")
_PHONE_CANDIDATE = re.compile(r"(?<!\w)(?:\+?\d[\d().\s-]{6,}\d)(?!\w)")
_URL = re.compile(r"(?i)\bhttps?://[^\s<>\]\[(){}]+")
_SOCIAL_HOSTS = (
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "t.me",
    "wa.me",
    "whatsapp.com",
)
_DOB_OR_AGE = re.compile(
    r"(?i)(?:\b(?:date\s+of\s+birth|birth\s*date|dob|born)\s*[:\-]"
    r"|\bage\s*[:\-]\s*\d{1,3}\b|\b\d{1,3}\s+years?\s+old\b)"
)
_STREET_ADDRESS = re.compile(
    r"(?i)(?:\b(?:address|postal\s+address|mailing\s+address|zip(?:\s+code)?|postal\s+code)\s*:"
    r"|\bP\.?\s*O\.?\s+Box\s+\d+\b"
    r"|\b\d{1,6}[A-Za-z]?\s+[\w.'-]+(?:\s+[\w.'-]+){0,5}\s+"
    r"(?:street|st\.?|road|rd\.?|avenue|ave\.?|boulevard|blvd\.?|lane|ln\.?|drive|dr\.?|"
    r"court|ct\.?|way|highway|hwy\.?|place|pl\.?|terrace|trail)\b"
    r"|\b(?:rua|avenida|av\.?|calle|via)\s+[\w.'-]+(?:\s+[\w.'-]+){0,5}\s*,?\s*\d{1,6}\b)"
)
_CONTACT_ONLY_LABEL = re.compile(
    r"(?i)^\s*(?:e-?mail|phone|telephone|tel\.?|mobile|cell|contact|linkedin|facebook|"
    r"instagram|twitter|whatsapp)\s*[:|\-]?\s*$"
)
_CONTACT_INFORMATION_LINE = re.compile(
    r"(?i)^\s*(?:e-?mail|phone|telephone|tel\.?|mobile|cell|contact|linkedin|facebook|"
    r"instagram|twitter|whatsapp)\s*[:|\-]"
)
_EMPTY_PUNCTUATION = re.compile(r"^[\s|,;:/\-–—()\[\]]*$")


def _looks_like_phone(candidate: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    groups = re.findall(r"\d+", candidate)
    if len(digits) < 10 or len(digits) > 15:
        return False
    return candidate.lstrip().startswith("+") or "(" in candidate or len(groups) >= 3


def _redact_phone(match: re.Match[str]) -> str:
    candidate = match.group(0)
    return "" if _looks_like_phone(candidate) else candidate


def _minimize_url(match: re.Match[str]) -> str:
    original = match.group(0)
    trailing = ""
    while original and original[-1] in ".,;:":
        trailing = original[-1] + trailing
        original = original[:-1]
    try:
        parts = urlsplit(original)
    except ValueError:
        return ""
    hostname = (parts.hostname or "").lower().removeprefix("www.")
    if any(hostname == host or hostname.endswith(f".{host}") for host in _SOCIAL_HOSTS):
        return trailing
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")) + trailing


def _minimize_line(line: str) -> str:
    if (
        _DOB_OR_AGE.search(line)
        or _STREET_ADDRESS.search(line)
        or _CONTACT_INFORMATION_LINE.search(line)
    ):
        return ""
    minimized = _EMAIL.sub("", line)
    minimized = _PHONE_CANDIDATE.sub(_redact_phone, minimized)
    minimized = _URL.sub(_minimize_url, minimized)
    minimized = re.sub(r"[ \t]+", " ", minimized).strip()
    minimized = re.sub(r"(?:\s*[|,;:/\-–—]\s*)+$", "", minimized).strip()
    if _CONTACT_ONLY_LABEL.fullmatch(minimized) or _EMPTY_PUNCTUATION.fullmatch(minimized):
        return ""
    return minimized


def _minimize_block(text: str) -> str:
    lines = (_minimize_line(line) for line in text.splitlines())
    return "\n".join(line for line in lines if line).strip()


def minimize_evidence_packet(evidence: EvidencePacket) -> ModelEvidencePacket:
    """Return bounded model-safe evidence without mutating or renumbering blocks.

    This deliberately handles only reliably detectable contact PII. It is not a
    general anonymizer and does not infer or erase professional facts.
    """

    if type(evidence) is not EvidencePacket:
        raise ProfileIntakeError("invalid_raw_evidence_packet")
    blocks: list[ModelEvidenceBlock] = []
    removed: list[str] = []
    for block in evidence.blocks:
        text = _minimize_block(block.text)
        if not text:
            removed.append(block.reference)
            continue
        blocks.append(ModelEvidenceBlock(reference=block.reference, text=text))
    return ModelEvidencePacket(
        document_reference=evidence.document_reference,
        document_kind=evidence.document_kind,
        document_format=evidence.document_format,
        blocks=tuple(blocks),
        removed_block_references=tuple(removed),
    )


def require_minimized_model_evidence(evidence: ModelEvidencePacket) -> None:
    """Fail closed if a caller manually labels detectable raw PII as model-safe."""

    if type(evidence) is not ModelEvidencePacket:
        raise ProfileIntakeError("invalid_model_evidence_packet")
    if any(_minimize_block(block.text) != block.text for block in evidence.blocks):
        raise ProfileIntakeError("model_evidence_not_minimized")

"""Versioned, deterministic policy for durable opportunity source capture."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from wahojobs.crawler.types import (
    BODY_OBSERVATION_EXPLICITLY_EMPTY,
    BODY_OBSERVATION_NOT_OBSERVED,
    BODY_OBSERVATION_PRESENT,
    BODY_OBSERVATION_STATES,
    MERIDIAL_GREENHOUSE_RECORD_CONTRACT_ID,
    CompanyCrawlResult,
    ProviderOutcome,
)


SOURCE_CAPTURE_CONTRACT_VERSION = "job_source_capture_v1"
SOURCE_PROMOTION_POLICY_VERSION = "job_source_promotion_v2"
SOURCE_CAPTURE_EVIDENCE_VERSION = "job_source_capture_evidence_v1"

SEMANTIC_AUTHORITY_LEGACY_ACCEPTED = "legacy_accepted"
SEMANTIC_AUTHORITY_PENDING = "pending"
SEMANTIC_AUTHORITY_VERSIONED_ACCEPTED = "versioned_accepted"
SEMANTIC_AUTHORITY_STATES = frozenset(
    {
        SEMANTIC_AUTHORITY_LEGACY_ACCEPTED,
        SEMANTIC_AUTHORITY_PENDING,
        SEMANTIC_AUTHORITY_VERSIONED_ACCEPTED,
    }
)

SEMANTIC_JOB_FIELD_NAMES = (
    "external_id",
    "source_hash",
    "title",
    "location",
    "department",
    "expertise",
    "commitment",
    "url",
    "opportunity_kind",
    "availability_basis",
    "include_in_live_market_estimate",
)

CAPTURE_QUALITY_HEALTHY_BODY = "healthy_body"
CAPTURE_QUALITY_METADATA_ONLY = "metadata_only"
CAPTURE_QUALITY_EMPTY = "empty"
CAPTURE_QUALITY_BLOCKED_OR_ERROR = "blocked_or_error"

PROMOTION_DECISION_PROMOTED = "promoted"
PROMOTION_DECISION_CONFIRMED = "confirmed"
PROMOTION_DECISION_HELD_DEGRADED = "held_degraded"
PROMOTION_DECISION_HELD_NON_AUTHORITATIVE = "held_non_authoritative"
PROMOTION_DECISION_HELD_SOURCE_CONFLICT = "held_source_conflict"

SOURCE_TIMESTAMP_ABSENT = "absent"
SOURCE_TIMESTAMP_VALID = "valid"
SOURCE_TIMESTAMP_INVALID = "invalid"

EVIDENCE_STATE_ACCEPTED_CURRENT = "accepted_current"
EVIDENCE_STATE_STALE_LAST_KNOWN_GOOD = "stale_last_known_good"
EVIDENCE_STATE_DEGRADED_LATEST = "degraded_latest"
EVIDENCE_STATE_LEGACY_ACCEPTED = "legacy_accepted"
EVIDENCE_STATE_MISSING = "missing"

EVIDENCE_REASON_CAPTURE_CONTRACT_CHANGED = "capture_contract_changed"
EVIDENCE_REASON_PROMOTION_POLICY_CHANGED = "promotion_policy_changed"
EVIDENCE_REASON_LATEST_CAPTURE_NOT_ACCEPTED = "latest_capture_not_accepted"
EVIDENCE_REASON_LATEST_SOURCE_ATTEMPT_FAILED = "latest_source_attempt_failed"

REASON_EMPTY_CONTENT = "empty_content"
REASON_BLOCKED_OR_ERROR_CONTENT = "blocked_or_error_content"
REASON_METADATA_CANNOT_REPLACE_BODY = "metadata_cannot_replace_body"
REASON_PROVIDER_OUTCOME_NOT_SUCCESS = "provider_outcome_not_success"
REASON_SNAPSHOT_INCOMPLETE = "snapshot_incomplete"
REASON_PAGINATION_INCOMPLETE = "pagination_incomplete"
REASON_SAMPLE_DATA = "sample_data"
REASON_RECORD_COUNT_MISMATCH = "record_count_mismatch"
REASON_SOURCE_TIMESTAMP_INVALID = "source_timestamp_invalid"
REASON_SOURCE_TIMESTAMP_MISSING = "source_timestamp_missing"
REASON_SOURCE_TIMESTAMP_REGRESSED = "source_timestamp_regressed"
REASON_SOURCE_TIMESTAMP_CONFLICT = "source_timestamp_conflict"
REASON_BODY_NOT_OBSERVED_CANNOT_REPLACE_BODY = (
    "body_not_observed_cannot_replace_body"
)
REASON_EXPLICIT_EMPTY_CANNOT_REPLACE_BODY = (
    "explicit_empty_cannot_replace_body"
)
REASON_BODY_OBSERVATION_REGRESSED = "body_observation_regressed"

MERIDIAL_GREENHOUSE_SOURCE_TYPE = "greenhouse-job-board-v1"
MERIDIAL_GREENHOUSE_PAYLOAD_SHAPE = (
    "greenhouse-job-board-v1:jobs+department-tree"
)
MERIDIAL_GREENHOUSE_SCHEMA_FINGERPRINT = (
    "greenhouse-job-board-v1:sha256:"
    "c35550b212c2c7ee0a54f6fa770122ec73f99c9b00606561cb8c5b1590d79901"
)
MERIDIAL_GREENHOUSE_ENDPOINT = (
    "https://boards-api.greenhouse.io/v1/boards/agency/jobs?content=true"
)
MERIDIAL_GREENHOUSE_JOB_HOSTS = frozenset(
    {"job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"}
)

_NUMERIC_TIMESTAMP = re.compile(r"^-?\d+(?:\.\d+)?$")
_BLOCKED_MARKERS = (
    "cf-chl-",
    "cloudflare ray id",
    "checking your browser before accessing",
    "verify you are human",
    "unusual traffic from your computer network",
)
_SHORT_ERROR_PREFIXES = (
    "access denied",
    "error 403",
    "error 404",
    "forbidden",
    "internal server error",
    "just a moment",
    "not found",
    "page not found",
    "request blocked",
    "security check required",
    "service unavailable",
    "temporarily unavailable",
    "too many requests",
)


@dataclass(frozen=True, slots=True)
class SourceCaptureContext:
    """Crawl evidence used by the promotion policy, captured without inference."""

    crawl_run_id: int | None
    provider_outcome: str
    used_sample_data: bool
    snapshot_complete: bool
    pagination_complete: bool
    empty_snapshot_validated: bool
    raw_record_count: int
    normalized_record_count: int
    candidate_count: int
    rejected_record_count: int
    payload_shape: str
    schema_fingerprint: str

    @classmethod
    def from_crawl_result(
        cls,
        crawl_run_id: int | None,
        crawl_result: CompanyCrawlResult,
    ) -> "SourceCaptureContext":
        return cls(
            crawl_run_id=crawl_run_id,
            provider_outcome=crawl_result.outcome.value,
            used_sample_data=bool(crawl_result.used_sample_data),
            snapshot_complete=bool(crawl_result.snapshot_complete),
            pagination_complete=bool(crawl_result.pagination_complete),
            empty_snapshot_validated=bool(crawl_result.empty_snapshot_validated),
            raw_record_count=int(crawl_result.raw_record_count),
            normalized_record_count=int(crawl_result.normalized_record_count),
            candidate_count=len(crawl_result.jobs),
            rejected_record_count=int(crawl_result.rejected_record_count),
            payload_shape=str(crawl_result.payload_shape or ""),
            schema_fingerprint=str(crawl_result.schema_fingerprint or ""),
        )

    def non_authoritative_reasons(self) -> tuple[str, ...]:
        reasons = []
        if self.provider_outcome != ProviderOutcome.SUCCESS.value:
            reasons.append(REASON_PROVIDER_OUTCOME_NOT_SUCCESS)
        if not self.snapshot_complete:
            reasons.append(REASON_SNAPSHOT_INCOMPLETE)
        if not self.pagination_complete:
            reasons.append(REASON_PAGINATION_INCOMPLETE)
        if self.used_sample_data:
            reasons.append(REASON_SAMPLE_DATA)
        if self.normalized_record_count != self.candidate_count:
            reasons.append(REASON_RECORD_COUNT_MISMATCH)
        return tuple(reasons)


@dataclass(frozen=True, slots=True)
class PreparedSourceCapture:
    body: str | None
    body_format: str | None
    metadata_json: str
    material_content_sha256: str
    quality: str
    source_updated_at: str | None
    source_timestamp_status: str
    comparable_source_timestamp: datetime | None


@dataclass(frozen=True, slots=True)
class PreparedRecordPromotionAttestation:
    contract_id: str
    body_observation: str
    authority_evidence_json: str

    @property
    def authoritative(self) -> bool:
        return bool(self.contract_id)


@dataclass(frozen=True, slots=True)
class PreparedSemanticSourceMaterial:
    job_fields_json: str
    semantic_material_sha256: str


@dataclass(frozen=True, slots=True)
class SourcePromotionDecision:
    decision: str
    reasons: tuple[str, ...]
    accepted_source_updated_at: str | None

    @property
    def accepted(self) -> bool:
        return self.decision in {
            PROMOTION_DECISION_PROMOTED,
            PROMOTION_DECISION_CONFIRMED,
        }


@dataclass(frozen=True, slots=True)
class SourceCapturePersistenceResult:
    capture_id: int
    material_content_sha256: str
    semantic_material_sha256: str
    promotion_decision: str

    @property
    def accepted(self) -> bool:
        return self.promotion_decision in {
            PROMOTION_DECISION_PROMOTED,
            PROMOTION_DECISION_CONFIRMED,
        }


def prepare_source_capture_v1(candidate) -> PreparedSourceCapture:
    body = normalize_source_body(candidate.source_body)
    body_format = candidate.source_body_format if body is not None else None
    if body is not None and body_format not in {
        "text/plain",
        "text/html",
        "text/markdown",
    }:
        raise ValueError("Unsupported source body format.")
    metadata = candidate.source_metadata or {}
    if type(metadata) is not dict:
        raise ValueError("Source metadata must be a dictionary.")
    metadata_json = canonical_source_metadata_json(metadata)
    material_hash = source_material_content_sha256(
        body,
        body_format,
        metadata,
    )
    raw_timestamp = normalize_source_timestamp(candidate.source_updated_at)
    timestamp_status, comparable_timestamp = parse_source_timestamp(raw_timestamp)

    if body is None:
        quality = CAPTURE_QUALITY_METADATA_ONLY if metadata else CAPTURE_QUALITY_EMPTY
    elif looks_blocked_or_error_like(body):
        quality = CAPTURE_QUALITY_BLOCKED_OR_ERROR
    else:
        quality = CAPTURE_QUALITY_HEALTHY_BODY

    return PreparedSourceCapture(
        body=body,
        body_format=body_format,
        metadata_json=metadata_json,
        material_content_sha256=material_hash,
        quality=quality,
        source_updated_at=raw_timestamp,
        source_timestamp_status=timestamp_status,
        comparable_source_timestamp=comparable_timestamp,
    )


def prepare_record_promotion_attestation(
    candidate,
    prepared: PreparedSourceCapture,
    context: SourceCaptureContext,
    *,
    provider: str,
    source_type: str,
) -> PreparedRecordPromotionAttestation:
    """Validate and canonicalize one provider-issued record attestation."""

    attestation = getattr(candidate, "record_promotion_attestation", None)
    if attestation is None:
        return PreparedRecordPromotionAttestation(
            contract_id="",
            body_observation=(
                BODY_OBSERVATION_PRESENT
                if prepared.body is not None
                else BODY_OBSERVATION_NOT_OBSERVED
            ),
            authority_evidence_json="{}",
        )
    try:
        contract_id = attestation.contract_id
        body_observation = attestation.body_observation
        evidence = attestation.authority_evidence
    except AttributeError as exc:
        raise ValueError("Malformed record promotion attestation.") from exc
    evidence_json = canonical_authority_evidence_json(evidence)
    prepared_attestation = PreparedRecordPromotionAttestation(
        contract_id=contract_id,
        body_observation=body_observation,
        authority_evidence_json=evidence_json,
    )
    _validate_record_promotion_attestation(
        prepared_attestation,
        candidate,
        prepared,
        context,
        provider=provider,
        source_type=source_type,
    )
    return prepared_attestation


def prepare_stored_record_promotion_attestation(
    *,
    contract_id,
    body_observation,
    authority_evidence_json,
    candidate,
    prepared: PreparedSourceCapture,
    context: SourceCaptureContext,
    provider: str,
    source_type: str,
) -> PreparedRecordPromotionAttestation:
    """Revalidate persisted evidence without consulting current provider code."""

    if type(contract_id) is not str or type(body_observation) is not str:
        raise ValueError("Malformed stored record promotion attestation.")
    if body_observation not in BODY_OBSERVATION_STATES:
        raise ValueError("Stored body observation is outside the closed contract.")
    try:
        evidence = json.loads(authority_evidence_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Stored authority evidence is not valid JSON.") from exc
    if canonical_authority_evidence_json(evidence) != authority_evidence_json:
        raise ValueError("Stored authority evidence is not canonical.")
    prepared_attestation = PreparedRecordPromotionAttestation(
        contract_id=contract_id,
        body_observation=body_observation,
        authority_evidence_json=authority_evidence_json,
    )
    if not contract_id:
        if evidence != {}:
            raise ValueError("Unattested captures cannot carry authority evidence.")
        return prepared_attestation
    _validate_record_promotion_attestation(
        prepared_attestation,
        candidate,
        prepared,
        context,
        provider=provider,
        source_type=source_type,
    )
    return prepared_attestation


def canonical_authority_evidence_json(evidence: dict) -> str:
    if type(evidence) is not dict:
        raise ValueError("Authority evidence must be a dictionary.")
    try:
        return json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Authority evidence must be JSON serializable.") from exc


def _validate_record_promotion_attestation(
    attestation: PreparedRecordPromotionAttestation,
    candidate,
    prepared: PreparedSourceCapture,
    context: SourceCaptureContext,
    *,
    provider: str,
    source_type: str,
) -> None:
    validator = RECORD_PROMOTION_CONTRACT_VALIDATORS.get(attestation.contract_id)
    if validator is None:
        raise ValueError("Unknown record promotion contract_id.")
    if attestation.body_observation not in BODY_OBSERVATION_STATES:
        raise ValueError("body_observation is outside the closed contract.")
    body_present = prepared.body is not None
    if (attestation.body_observation == BODY_OBSERVATION_PRESENT) != body_present:
        raise ValueError("body_observation does not match captured body presence.")
    validator(
        attestation,
        candidate,
        prepared,
        context,
        provider=provider,
        source_type=source_type,
    )


def _validate_meridial_greenhouse_record_v1(
    attestation: PreparedRecordPromotionAttestation,
    candidate,
    prepared: PreparedSourceCapture,
    context: SourceCaptureContext,
    *,
    provider: str,
    source_type: str,
) -> None:
    try:
        evidence = json.loads(attestation.authority_evidence_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Malformed Meridial Greenhouse authority evidence.") from exc
    expected_keys = {
        "authoritative_endpoint",
        "greenhouse_job_id",
        "record_shape",
        "required_record_shape_validated",
        "schema_fingerprint",
        "stable_identity_validated",
        "updated_at",
    }
    if set(evidence) != expected_keys:
        raise ValueError("Meridial Greenhouse authority evidence is not closed.")
    job_id = evidence["greenhouse_job_id"]
    if type(job_id) is not int or job_id < 1:
        raise ValueError("Greenhouse job identity is invalid.")
    if (
        provider != "meridial"
        or source_type != MERIDIAL_GREENHOUSE_SOURCE_TYPE
        or evidence["authoritative_endpoint"] != MERIDIAL_GREENHOUSE_ENDPOINT
        or evidence["record_shape"] != MERIDIAL_GREENHOUSE_SOURCE_TYPE
        or evidence["schema_fingerprint"]
        != MERIDIAL_GREENHOUSE_SCHEMA_FINGERPRINT
        or evidence["required_record_shape_validated"] is not True
        or evidence["stable_identity_validated"] is not True
        or context.payload_shape != MERIDIAL_GREENHOUSE_PAYLOAD_SHAPE
        or context.schema_fingerprint != MERIDIAL_GREENHOUSE_SCHEMA_FINGERPRINT
        or context.candidate_count < 1
        or context.raw_record_count
        != context.normalized_record_count + context.rejected_record_count
        or str(job_id) != candidate.external_id
        or evidence["updated_at"] != prepared.source_updated_at
    ):
        raise ValueError("Meridial Greenhouse authority evidence is inconsistent.")
    if not isinstance(candidate.title, str) or not candidate.title.strip():
        raise ValueError("Greenhouse title is not a valid observed record field.")
    metadata = json.loads(prepared.metadata_json)
    if (
        type(metadata.get("departments")) is not list
        or type(metadata.get("offices")) is not list
    ):
        raise ValueError("Greenhouse required record collections are missing.")
    try:
        parsed_url = urlparse(candidate.url)
        port = parsed_url.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Greenhouse stable job URL is malformed.") from exc
    if (
        parsed_url.scheme != "https"
        or (parsed_url.hostname or "").casefold()
        not in MERIDIAL_GREENHOUSE_JOB_HOSTS
        or port is not None
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
        or parsed_url.path.rstrip("/") != f"/agency/jobs/{job_id}"
    ):
        raise ValueError("Greenhouse stable job identity is inconsistent.")


def canonical_source_metadata_json(metadata: dict) -> str:
    if type(metadata) is not dict:
        raise ValueError("Source metadata must be a dictionary.")
    try:
        return json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Source metadata must be JSON serializable.") from exc


def source_material_content_sha256(
    body: str | None,
    body_format: str | None,
    metadata: dict,
) -> str:
    material_payload = json.dumps(
        {
            "body": body,
            "body_format": body_format,
            "metadata": metadata,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material_payload.encode("utf-8")).hexdigest()


def semantic_job_fields(candidate, classification: dict) -> dict:
    """Return the job fields that participate in enrichment semantic input."""

    return normalize_semantic_job_fields(
        {
            "external_id": candidate.external_id,
            "source_hash": candidate.source_hash,
            "title": candidate.title,
            "location": candidate.location,
            "department": candidate.department,
            "expertise": candidate.expertise,
            "commitment": candidate.commitment,
            "url": candidate.url,
            "opportunity_kind": classification["opportunity_kind"],
            "availability_basis": classification["availability_basis"],
            "include_in_live_market_estimate": classification[
                "include_in_live_market_estimate"
            ],
        }
    )


def semantic_job_fields_from_row(row) -> dict:
    return normalize_semantic_job_fields(
        {name: row[name] for name in SEMANTIC_JOB_FIELD_NAMES}
    )


def normalize_semantic_job_fields(fields: dict) -> dict:
    if type(fields) is not dict or set(fields) != set(SEMANTIC_JOB_FIELD_NAMES):
        raise ValueError("Semantic job fields do not match the closed contract.")
    include = fields["include_in_live_market_estimate"]
    if type(include) not in {bool, int} or include not in {0, 1}:
        raise ValueError("Semantic job live-market authority must be boolean.")
    normalized = {name: fields[name] for name in SEMANTIC_JOB_FIELD_NAMES}
    normalized["include_in_live_market_estimate"] = bool(include)
    return normalized


def canonical_semantic_job_fields_json(fields: dict) -> str:
    return json.dumps(
        normalize_semantic_job_fields(fields),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def prepare_semantic_source_material(
    candidate,
    classification: dict,
    prepared: PreparedSourceCapture,
    *,
    provider: str,
    source_type: str,
) -> PreparedSemanticSourceMaterial:
    fields = semantic_job_fields(candidate, classification)
    metadata = json.loads(prepared.metadata_json)
    return PreparedSemanticSourceMaterial(
        job_fields_json=canonical_semantic_job_fields_json(fields),
        semantic_material_sha256=semantic_source_material_sha256(
            fields,
            provider=provider,
            source_type=source_type,
            source_url=candidate.url,
            source_external_id=candidate.external_id,
            body=prepared.body,
            body_format=prepared.body_format,
            metadata=metadata,
        ),
    )


def semantic_source_material_sha256(
    job_fields: dict,
    *,
    provider: str,
    source_type: str,
    source_url: str,
    source_external_id: str | None,
    body: str | None,
    body_format: str | None,
    metadata: dict,
) -> str:
    """Hash every persisted field that can affect enrichment semantic input."""

    if type(metadata) is not dict:
        raise ValueError("Source metadata must be a dictionary.")
    payload = {
        "job": normalize_semantic_job_fields(job_fields),
        "source": {
            "provider": provider,
            "source_type": source_type,
            "source_url": source_url,
            "external_id": source_external_id,
            "body": body,
            "body_format": body_format,
            "metadata": metadata,
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def decide_source_promotion_v1(
    prepared: PreparedSourceCapture,
    context: SourceCaptureContext,
    accepted_row,
    *,
    same_accepted_semantic_material: bool = False,
    record_attestation: PreparedRecordPromotionAttestation | None = None,
    accepted_record_attestation: PreparedRecordPromotionAttestation | None = None,
) -> SourcePromotionDecision:
    """Decide whether this capture may replace or reconfirm accepted evidence."""

    if prepared.quality == CAPTURE_QUALITY_EMPTY:
        return SourcePromotionDecision(
            PROMOTION_DECISION_HELD_DEGRADED,
            (REASON_EMPTY_CONTENT,),
            _accepted_timestamp(accepted_row),
        )
    if prepared.quality == CAPTURE_QUALITY_BLOCKED_OR_ERROR:
        return SourcePromotionDecision(
            PROMOTION_DECISION_HELD_DEGRADED,
            (REASON_BLOCKED_OR_ERROR_CONTENT,),
            _accepted_timestamp(accepted_row),
        )

    authority_reasons = context.non_authoritative_reasons()
    if authority_reasons:
        return SourcePromotionDecision(
            PROMOTION_DECISION_HELD_NON_AUTHORITATIVE,
            authority_reasons,
            _accepted_timestamp(accepted_row),
        )

    if accepted_row is None:
        accepted_timestamp = (
            prepared.source_updated_at
            if prepared.source_timestamp_status == SOURCE_TIMESTAMP_VALID
            else None
        )
        notes = (
            (REASON_SOURCE_TIMESTAMP_INVALID,)
            if prepared.source_timestamp_status == SOURCE_TIMESTAMP_INVALID
            else ()
        )
        return SourcePromotionDecision(
            PROMOTION_DECISION_PROMOTED,
            notes,
            accepted_timestamp,
        )

    accepted_has_body = bool(normalize_source_body(accepted_row["body"]))
    if (
        accepted_has_body
        and prepared.quality == CAPTURE_QUALITY_METADATA_ONLY
    ):
        return SourcePromotionDecision(
            PROMOTION_DECISION_HELD_DEGRADED,
            (REASON_METADATA_CANNOT_REPLACE_BODY,),
            _accepted_timestamp(accepted_row),
        )

    same_material = (
        prepared.material_content_sha256
        == accepted_row["material_content_sha256"]
    )
    # Confirmation is deliberately stricter than source-body equality: every
    # field that can reach enrichment must still match accepted material.
    if same_material and same_accepted_semantic_material:
        return SourcePromotionDecision(
            PROMOTION_DECISION_CONFIRMED,
            _timestamp_notes_for_confirmation(prepared, accepted_row),
            _confirmed_timestamp(prepared, accepted_row),
        )

    if (
        not accepted_has_body
        and prepared.quality == CAPTURE_QUALITY_HEALTHY_BODY
    ):
        accepted_status, accepted_timestamp = parse_source_timestamp(
            _accepted_timestamp(accepted_row)
        )
        if (
            accepted_status == SOURCE_TIMESTAMP_VALID
            and prepared.source_timestamp_status == SOURCE_TIMESTAMP_VALID
            and prepared.comparable_source_timestamp < accepted_timestamp
        ):
            return SourcePromotionDecision(
                PROMOTION_DECISION_HELD_SOURCE_CONFLICT,
                (REASON_SOURCE_TIMESTAMP_REGRESSED,),
                _accepted_timestamp(accepted_row),
            )
        notes = (
            (REASON_SOURCE_TIMESTAMP_INVALID,)
            if prepared.source_timestamp_status == SOURCE_TIMESTAMP_INVALID
            else ()
        )
        return SourcePromotionDecision(
            PROMOTION_DECISION_PROMOTED,
            notes,
            (
                prepared.source_updated_at
                if prepared.source_timestamp_status == SOURCE_TIMESTAMP_VALID
                else _accepted_timestamp(accepted_row)
            ),
        )

    timestamp_conflict = _changed_content_timestamp_conflict(prepared, accepted_row)
    if timestamp_conflict is not None:
        return SourcePromotionDecision(
            PROMOTION_DECISION_HELD_SOURCE_CONFLICT,
            (timestamp_conflict,),
            _accepted_timestamp(accepted_row),
        )

    accepted_timestamp = (
        prepared.source_updated_at
        if prepared.source_timestamp_status == SOURCE_TIMESTAMP_VALID
        else _accepted_timestamp(accepted_row)
    )
    return SourcePromotionDecision(
        PROMOTION_DECISION_PROMOTED,
        (),
        accepted_timestamp,
    )


def decide_source_promotion_v2(
    prepared: PreparedSourceCapture,
    context: SourceCaptureContext,
    accepted_row,
    *,
    same_accepted_semantic_material: bool = False,
    record_attestation: PreparedRecordPromotionAttestation | None = None,
    accepted_record_attestation: PreparedRecordPromotionAttestation | None = None,
) -> SourcePromotionDecision:
    """Add strict per-record authority while preserving generic V1 behavior."""

    if record_attestation is None or not record_attestation.authoritative:
        return decide_source_promotion_v1(
            prepared,
            context,
            accepted_row,
            same_accepted_semantic_material=same_accepted_semantic_material,
        )
    if record_attestation.contract_id != MERIDIAL_GREENHOUSE_RECORD_CONTRACT_ID:
        raise ValueError("Promotion policy received an unsupported contract_id.")

    if prepared.quality == CAPTURE_QUALITY_EMPTY:
        return SourcePromotionDecision(
            PROMOTION_DECISION_HELD_DEGRADED,
            (REASON_EMPTY_CONTENT,),
            _accepted_timestamp(accepted_row),
        )
    if prepared.quality == CAPTURE_QUALITY_BLOCKED_OR_ERROR:
        return SourcePromotionDecision(
            PROMOTION_DECISION_HELD_DEGRADED,
            (REASON_BLOCKED_OR_ERROR_CONTENT,),
            _accepted_timestamp(accepted_row),
        )

    record_authority_reasons = []
    if context.used_sample_data:
        record_authority_reasons.append(REASON_SAMPLE_DATA)
    if context.normalized_record_count != context.candidate_count:
        record_authority_reasons.append(REASON_RECORD_COUNT_MISMATCH)
    if context.provider_outcome == ProviderOutcome.CONTRACT_DRIFT.value:
        record_authority_reasons.append(REASON_PROVIDER_OUTCOME_NOT_SUCCESS)
    if record_authority_reasons:
        return SourcePromotionDecision(
            PROMOTION_DECISION_HELD_NON_AUTHORITATIVE,
            tuple(record_authority_reasons),
            _accepted_timestamp(accepted_row),
        )

    if prepared.source_timestamp_status != SOURCE_TIMESTAMP_VALID:
        reason = (
            REASON_SOURCE_TIMESTAMP_INVALID
            if prepared.source_timestamp_status == SOURCE_TIMESTAMP_INVALID
            else REASON_SOURCE_TIMESTAMP_MISSING
        )
        return SourcePromotionDecision(
            PROMOTION_DECISION_HELD_SOURCE_CONFLICT,
            (reason,),
            _accepted_timestamp(accepted_row),
        )

    if accepted_row is None:
        return SourcePromotionDecision(
            PROMOTION_DECISION_PROMOTED,
            (),
            prepared.source_updated_at,
        )

    accepted_has_body = bool(normalize_source_body(accepted_row["body"]))
    if accepted_has_body and record_attestation.body_observation != BODY_OBSERVATION_PRESENT:
        reason = (
            REASON_EXPLICIT_EMPTY_CANNOT_REPLACE_BODY
            if record_attestation.body_observation
            == BODY_OBSERVATION_EXPLICITLY_EMPTY
            else REASON_BODY_NOT_OBSERVED_CANNOT_REPLACE_BODY
        )
        return SourcePromotionDecision(
            PROMOTION_DECISION_HELD_DEGRADED,
            (reason,),
            _accepted_timestamp(accepted_row),
        )

    accepted_status, accepted_timestamp = parse_source_timestamp(
        _accepted_timestamp(accepted_row)
    )
    if (
        accepted_status == SOURCE_TIMESTAMP_VALID
        and prepared.comparable_source_timestamp < accepted_timestamp
    ):
        return SourcePromotionDecision(
            PROMOTION_DECISION_HELD_SOURCE_CONFLICT,
            (REASON_SOURCE_TIMESTAMP_REGRESSED,),
            _accepted_timestamp(accepted_row),
        )

    same_material = (
        prepared.material_content_sha256
        == accepted_row["material_content_sha256"]
        and same_accepted_semantic_material
    )
    if (
        not accepted_has_body
        and accepted_record_attestation is not None
        and accepted_record_attestation.body_observation
        == BODY_OBSERVATION_EXPLICITLY_EMPTY
        and record_attestation.body_observation == BODY_OBSERVATION_NOT_OBSERVED
    ):
        return SourcePromotionDecision(
            PROMOTION_DECISION_HELD_DEGRADED,
            (REASON_BODY_OBSERVATION_REGRESSED,),
            _accepted_timestamp(accepted_row),
        )
    if same_material:
        return SourcePromotionDecision(
            PROMOTION_DECISION_CONFIRMED,
            (),
            prepared.source_updated_at,
        )
    if (
        accepted_status == SOURCE_TIMESTAMP_VALID
        and prepared.comparable_source_timestamp == accepted_timestamp
    ):
        return SourcePromotionDecision(
            PROMOTION_DECISION_HELD_SOURCE_CONFLICT,
            (REASON_SOURCE_TIMESTAMP_CONFLICT,),
            _accepted_timestamp(accepted_row),
        )
    return SourcePromotionDecision(
        PROMOTION_DECISION_PROMOTED,
        (),
        prepared.source_updated_at,
    )


# Historical capture and policy implementations are permanently pinned.  A
# future current-version bump must add a new literal mapping rather than making
# old captures follow mutable current behavior.
SOURCE_CAPTURE_CONTRACT_PREPARERS = {
    "job_source_capture_v1": prepare_source_capture_v1,
}
RECORD_PROMOTION_CONTRACT_VALIDATORS = {
    "meridial_greenhouse_record_v1": _validate_meridial_greenhouse_record_v1,
}
SOURCE_PROMOTION_POLICY_DECIDERS = {
    "job_source_promotion_v1": decide_source_promotion_v1,
    "job_source_promotion_v2": decide_source_promotion_v2,
}

# Current write aliases remain convenient for callers while replay uses the
# literal historical maps above.
prepare_source_capture = prepare_source_capture_v1
decide_source_promotion = decide_source_promotion_v2


def normalize_source_body(value) -> str | None:
    if value is None:
        return None
    value = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    return value or None


def normalize_source_timestamp(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def parse_source_timestamp(value: str | None) -> tuple[str, datetime | None]:
    if value is None:
        return SOURCE_TIMESTAMP_ABSENT, None
    try:
        if _NUMERIC_TIMESTAMP.fullmatch(value):
            numeric = float(value)
            magnitude = abs(numeric)
            if magnitude >= 100_000_000_000_000:
                numeric /= 1_000_000
            elif magnitude >= 100_000_000_000:
                numeric /= 1_000
            parsed = datetime.fromtimestamp(numeric, tz=timezone.utc)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return SOURCE_TIMESTAMP_INVALID, None
    return SOURCE_TIMESTAMP_VALID, parsed


def looks_blocked_or_error_like(body: str) -> bool:
    lowered = " ".join(body.casefold().split())
    visible_text = " ".join(re.sub(r"<[^>]+>", " ", body).casefold().split())
    if any(marker in lowered for marker in _BLOCKED_MARKERS):
        return True
    return len(visible_text) <= 2_000 and visible_text.startswith(
        _SHORT_ERROR_PREFIXES
    )


def _accepted_timestamp(accepted_row) -> str | None:
    return None if accepted_row is None else accepted_row["source_updated_at"]


def _timestamp_notes_for_confirmation(
    prepared: PreparedSourceCapture,
    accepted_row,
) -> tuple[str, ...]:
    if prepared.source_timestamp_status == SOURCE_TIMESTAMP_INVALID:
        return (REASON_SOURCE_TIMESTAMP_INVALID,)
    accepted_status, accepted_timestamp = parse_source_timestamp(
        _accepted_timestamp(accepted_row)
    )
    if (
        accepted_status == SOURCE_TIMESTAMP_VALID
        and prepared.source_timestamp_status == SOURCE_TIMESTAMP_VALID
        and prepared.comparable_source_timestamp < accepted_timestamp
    ):
        return (REASON_SOURCE_TIMESTAMP_REGRESSED,)
    return ()


def _confirmed_timestamp(
    prepared: PreparedSourceCapture,
    accepted_row,
) -> str | None:
    accepted_raw = _accepted_timestamp(accepted_row)
    accepted_status, accepted_timestamp = parse_source_timestamp(accepted_raw)
    if prepared.source_timestamp_status != SOURCE_TIMESTAMP_VALID:
        return accepted_raw
    if (
        accepted_status == SOURCE_TIMESTAMP_VALID
        and prepared.comparable_source_timestamp < accepted_timestamp
    ):
        return accepted_raw
    return prepared.source_updated_at


def _changed_content_timestamp_conflict(
    prepared: PreparedSourceCapture,
    accepted_row,
) -> str | None:
    accepted_status, accepted_timestamp = parse_source_timestamp(
        _accepted_timestamp(accepted_row)
    )
    if prepared.source_timestamp_status == SOURCE_TIMESTAMP_INVALID:
        return REASON_SOURCE_TIMESTAMP_INVALID
    if (
        accepted_status == SOURCE_TIMESTAMP_VALID
        and prepared.source_timestamp_status == SOURCE_TIMESTAMP_ABSENT
    ):
        return REASON_SOURCE_TIMESTAMP_MISSING
    if (
        accepted_status == SOURCE_TIMESTAMP_VALID
        and prepared.source_timestamp_status == SOURCE_TIMESTAMP_VALID
    ):
        if prepared.comparable_source_timestamp < accepted_timestamp:
            return REASON_SOURCE_TIMESTAMP_REGRESSED
        if prepared.comparable_source_timestamp == accepted_timestamp:
            return REASON_SOURCE_TIMESTAMP_CONFLICT
    return None

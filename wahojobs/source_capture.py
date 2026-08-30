"""Versioned, deterministic policy for durable opportunity source capture."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome


SOURCE_CAPTURE_CONTRACT_VERSION = "job_source_capture_v1"
SOURCE_PROMOTION_POLICY_VERSION = "job_source_promotion_v1"
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


# Historical capture and policy implementations are permanently pinned.  A
# future current-version bump must add a new literal mapping rather than making
# old captures follow mutable current behavior.
SOURCE_CAPTURE_CONTRACT_PREPARERS = {
    "job_source_capture_v1": prepare_source_capture_v1,
}
SOURCE_PROMOTION_POLICY_DECIDERS = {
    "job_source_promotion_v1": decide_source_promotion_v1,
}

# Current write aliases remain convenient for callers while replay uses the
# literal historical maps above.
prepare_source_capture = prepare_source_capture_v1
decide_source_promotion = decide_source_promotion_v1


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

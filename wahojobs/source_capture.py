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
    MERCOR_RECORD_CONTRACT_ID,
    PROVIDER_DETAIL_RECORD_CONTRACT_ID,
    CompanyCrawlResult,
    ProviderOutcome,
)


SOURCE_CAPTURE_CONTRACT_VERSION = "job_source_capture_v1"
SOURCE_PROMOTION_POLICY_VERSION = "job_source_promotion_v2"
MERCOR_PROMOTION_POLICY_VERSION = "mercor_record_promotion_v2"
DATAANNOTATION_CODING_RECORD_CONTRACT_ID = "dataannotation_coding_evergreen_record_v1"
DATAANNOTATION_ROLE_RECORD_CONTRACT_ID = "dataannotation_evergreen_role_record_v2"
DATAANNOTATION_ROLES_SHAPE = "dataannotation_evergreen_roles_v2"
DATAFORCE_INDEX_DETAIL_RECORD_CONTRACT_ID = "dataforce_index_detail_record_v1"
SURGE_REMOTE_WORKFORCE_RECORD_CONTRACT_ID = "surge_remote_workforce_record_v1"
HANDSHAKE_PUBLIC_CMS_RECORD_CONTRACT_ID = "handshake_public_cms_record_v1"
OUTLIER_INDEX_DETAIL_RECORD_CONTRACT_ID = "outlier_index_detail_record_v1"
PROVIDER_DETAIL_PROMOTION_POLICY_VERSION = "provider_detail_content_promotion_v1"
MERCOR_DETAIL_PROMOTION_POLICY_VERSION = "mercor_detail_content_promotion_v2"
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


def _validate_mercor_public_active_record_v1(
    attestation, candidate, prepared, context, *, provider, source_type,
):
    evidence = json.loads(attestation.authority_evidence_json)
    if set(evidence) != {
        "authoritative_endpoint", "listing_id", "status", "deleted_at",
        "is_private", "required_record_shape_validated",
    }:
        raise ValueError("Mercor record authority evidence is not closed.")
    identity = evidence["listing_id"]
    if (
        provider != "mercor" or source_type != "mercor-marketplace"
        or evidence["authoritative_endpoint"] != "https://aws.api.mercor.com/work/listings-explore-page"
        or not isinstance(identity, str)
        or re.fullmatch(r"[A-Za-z0-9_-]+", identity) is None
        or identity != candidate.external_id
        or candidate.url != f"https://work.mercor.com/jobs/{identity}"
        or evidence["status"] != "active"
        or evidence["deleted_at"] is not None
        or evidence["is_private"] is not False
        or evidence["required_record_shape_validated"] is not True
        or not isinstance(candidate.title, str) or not candidate.title.strip()
        or context.payload_shape != "mercor-marketplace:listings:v1"
        or context.schema_fingerprint != "mercor-public-active-record:v1"
        or context.candidate_count < 1
        or context.raw_record_count != context.normalized_record_count + context.rejected_record_count
    ):
        raise ValueError("Mercor public active record evidence is inconsistent.")


def _validate_dataannotation_coding_record_v1(
    attestation, candidate, prepared, context, *, provider, source_type,
):
    from wahojobs.classification import (
        AVAILABILITY_BASIS_EVERGREEN_PAGE, OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
    )
    from wahojobs.crawler.providers.dataannotation import coding_role_evidence
    evidence = json.loads(attestation.authority_evidence_json)
    expected = {"requested_url", "final_url", "role_slug", "title", "application_url", "body_sha256"}
    if (set(evidence) != expected or provider != "dataannotation"
            or source_type != "evergreen-application-pages"
            or attestation.body_observation != BODY_OBSERVATION_PRESENT
            or prepared.body_format != "text/html" or not prepared.body
            or candidate.external_id != "dataannotation::coding"
            or candidate.url != evidence["final_url"]
            or evidence["requested_url"] != "https://www.dataannotation.tech/coding"
            or evidence["role_slug"] != "software-engineer"
            or candidate.opportunity_kind != OPPORTUNITY_KIND_EVERGREEN_APPLICATION
            or candidate.availability_basis != AVAILABILITY_BASIS_EVERGREEN_PAGE
            or candidate.include_in_live_market_estimate is not False
            or context.provider_outcome not in {ProviderOutcome.SUCCESS.value, ProviderOutcome.PARTIAL.value}
            or context.used_sample_data or context.snapshot_complete or context.pagination_complete
            or context.crawl_run_id is None
            or context.candidate_count < 1 or context.normalized_record_count != context.candidate_count
            or context.raw_record_count < context.normalized_record_count
            or context.payload_shape not in {DATAANNOTATION_CODING_RECORD_CONTRACT_ID, DATAANNOTATION_ROLES_SHAPE}
            or context.schema_fingerprint != context.payload_shape):
        raise ValueError("DataAnnotation coding record authority is inconsistent.")
    title, application_url = coding_role_evidence(prepared.body, candidate.url)
    metadata = json.loads(prepared.metadata_json)
    if (candidate.title != title or evidence["title"] != title
            or metadata != {"application_url": application_url}
            or evidence["application_url"] != application_url
            or evidence["body_sha256"] != hashlib.sha256(prepared.body.encode("utf-8")).hexdigest()):
        raise ValueError("DataAnnotation coding body and authority disagree.")


def _validate_dataannotation_role_record_v2(
    attestation, candidate, prepared, context, *, provider, source_type,
):
    from wahojobs.classification import (
        AVAILABILITY_BASIS_EVERGREEN_PAGE, OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
    )
    from wahojobs.crawler.providers.dataannotation import ROLE_IDENTITIES, evergreen_role_evidence
    evidence = json.loads(attestation.authority_evidence_json)
    domain = candidate.external_id.removeprefix("dataannotation::")
    identity = ROLE_IDENTITIES.get(domain)
    expected = {"requested_url", "final_url", "role_slug", "title", "application_url", "body_sha256"}
    if (set(evidence) != expected or identity is None
            or candidate.external_id != "dataannotation::" + domain
            or provider != "dataannotation" or source_type != "evergreen-application-pages"
            or attestation.body_observation != BODY_OBSERVATION_PRESENT
            or prepared.body_format != "text/html" or not prepared.body
            or candidate.url != evidence["final_url"]
            or evidence["requested_url"] != f"https://www.dataannotation.tech/{domain}"
            or evidence["role_slug"] != identity[0]
            or candidate.location != "Remote"
            or candidate.opportunity_kind != OPPORTUNITY_KIND_EVERGREEN_APPLICATION
            or candidate.availability_basis != AVAILABILITY_BASIS_EVERGREEN_PAGE
            or candidate.include_in_live_market_estimate is not False
            or context.provider_outcome not in {ProviderOutcome.SUCCESS.value, ProviderOutcome.PARTIAL.value}
            or context.used_sample_data or context.snapshot_complete or context.pagination_complete
            or context.crawl_run_id is None or context.candidate_count < 1
            or context.normalized_record_count != context.candidate_count
            or context.raw_record_count < context.normalized_record_count
            or context.payload_shape != DATAANNOTATION_ROLES_SHAPE
            or context.schema_fingerprint != DATAANNOTATION_ROLES_SHAPE):
        raise ValueError("DataAnnotation role authority is inconsistent.")
    title, application_url = evergreen_role_evidence(domain, prepared.body, candidate.url)
    if (candidate.title != title or evidence["title"] != title
            or json.loads(prepared.metadata_json) != {"application_url": application_url}
            or evidence["application_url"] != application_url
            or evidence["body_sha256"] != hashlib.sha256(prepared.body.encode("utf-8")).hexdigest()):
        raise ValueError("DataAnnotation role body and authority disagree.")


def _validate_dataforce_index_detail_record_v1(
    attestation, candidate, prepared, context, *, provider, source_type,
):
    from wahojobs.classification import AVAILABILITY_BASIS_PUBLIC_PAGE, OPPORTUNITY_KIND_LIVE_POSTING
    from wahojobs.crawler.providers.dataforce import (
        parse_job_block, detail_role_evidence, QUALIFIED_DETAIL_PATHS,
    )
    evidence = json.loads(attestation.authority_evidence_json)
    metadata = json.loads(prepared.metadata_json)
    expected = {"external_id", "index_page_url", "index_page_sha256",
                "index_card_sha256", "detail_url", "detail_sha256", "title", "application_url"}
    required_meta = {"index_card_html", "index_page_url", "index_page_sha256", "application_url"}
    if (set(evidence) != expected or not required_meta <= set(metadata)
            or provider != "dataforce" or source_type != "dataforce-community-html"
            or attestation.body_observation != BODY_OBSERVATION_PRESENT
            or prepared.body_format != "text/html" or not prepared.body
            or context.provider_outcome != ProviderOutcome.PARTIAL.value
            or context.used_sample_data or context.snapshot_complete or context.pagination_complete
            or context.crawl_run_id is None or context.candidate_count < 1
            or context.normalized_record_count != context.candidate_count
            or context.raw_record_count < context.normalized_record_count
            or context.payload_shape != DATAFORCE_INDEX_DETAIL_RECORD_CONTRACT_ID
            or context.schema_fingerprint != DATAFORCE_INDEX_DETAIL_RECORD_CONTRACT_ID
            or candidate.opportunity_kind != OPPORTUNITY_KIND_LIVE_POSTING
            or candidate.availability_basis != AVAILABILITY_BASIS_PUBLIC_PAGE
            or candidate.include_in_live_market_estimate is not True
            or evidence["external_id"] != candidate.external_id
            or evidence["detail_url"] != candidate.url
            or evidence["title"] != candidate.title
            or evidence["index_page_url"] != metadata["index_page_url"]
            or evidence["index_page_sha256"] != metadata["index_page_sha256"]
            or evidence["application_url"] != metadata["application_url"]
            or not re.fullmatch(r"[a-f0-9]{64}", str(evidence["index_page_sha256"]))
            or urlparse(candidate.url).path not in QUALIFIED_DETAIL_PATHS):
        raise ValueError("DataForce index/detail record authority is inconsistent.")
    card = metadata["index_card_html"]
    if (not isinstance(card, str) or not card
            or evidence["index_card_sha256"] != hashlib.sha256(card.encode()).hexdigest()
            or evidence["detail_sha256"] != hashlib.sha256(prepared.body.encode()).hexdigest()):
        raise ValueError("DataForce index/detail source body changed.")
    index = parse_job_block(card, metadata["index_page_url"])
    if (index is None or (index.external_id, index.url, index.title,
            index.location, index.commitment, index.department, index.expertise)
            != (candidate.external_id, candidate.url, candidate.title,
                candidate.location, candidate.commitment, candidate.department, candidate.expertise)
            or not set((index.source_metadata or {}).items()) <= set(metadata.items())):
        raise ValueError("DataForce exact index card disagrees with the record.")
    if detail_role_evidence(index, prepared.body) != evidence["application_url"]:
        raise ValueError("DataForce detail and application authority disagree.")


def _validate_surge_remote_workforce_record_v1(
    attestation, candidate, prepared, context, *, provider, source_type,
):
    from wahojobs.crawler.providers.surge import (
        WorkforceRecord, extract_workforce_records, parse_workforce_detail,
    )
    from wahojobs.classification import (
        AVAILABILITY_BASIS_PUBLIC_PAGE, OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY,
    )

    evidence = json.loads(attestation.authority_evidence_json)
    metadata = json.loads(prepared.metadata_json)
    required = {'external_id', 'url', 'title', 'index_page_sha256',
                'detail_page_sha256', 'application_email'}
    record_data = metadata.get('index_record')
    index_html = metadata.get('index_page_html')
    detail_html = metadata.get('detail_page_html')
    if (set(evidence) != required or type(record_data) is not dict
            or set(record_data) != {'slug', 'url', 'fields', 'index_text'}
            or type(record_data['fields']) is not dict
            or type(index_html) is not str or not index_html
            or type(detail_html) is not str or not detail_html
            or provider != 'surge' or source_type != 'public-worker-pages'
            or attestation.body_observation != BODY_OBSERVATION_PRESENT
            or prepared.body_format != 'text/plain' or not prepared.body
            or context.provider_outcome != ProviderOutcome.PARTIAL.value
            or context.used_sample_data or context.snapshot_complete
            or context.pagination_complete or context.crawl_run_id is None
            or context.candidate_count < 1
            or context.normalized_record_count != context.candidate_count
            or context.raw_record_count < context.candidate_count
            or context.payload_shape != SURGE_REMOTE_WORKFORCE_RECORD_CONTRACT_ID
            or context.schema_fingerprint != SURGE_REMOTE_WORKFORCE_RECORD_CONTRACT_ID
            or candidate.opportunity_kind != OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY
            or candidate.availability_basis != AVAILABILITY_BASIS_PUBLIC_PAGE
            or candidate.include_in_live_market_estimate is not False
            or candidate.location != 'Remote'
            or evidence['external_id'] != candidate.external_id
            or evidence['url'] != candidate.url or evidence['title'] != candidate.title
            or evidence['index_page_sha256'] != metadata.get('index_page_sha256')
            or evidence['index_page_sha256'] != hashlib.sha256(index_html.encode()).hexdigest()
            or evidence['application_email'] != 'talent@surgehq.ai'
            or not re.fullmatch(r'[a-f0-9]{64}', str(evidence['index_page_sha256']))
            or evidence['detail_page_sha256'] != hashlib.sha256(detail_html.encode()).hexdigest()):
        raise ValueError('Surge public workforce record authority is inconsistent.')
    record = WorkforceRecord(**record_data)
    indexed = [item for item in extract_workforce_records(
        index_html, 'https://surgehq.ai/workforce') if item.slug == record.slug]
    if len(indexed) != 1 or indexed[0] != record:
        raise ValueError('Surge exact index record is not attested.')
    try:
        parsed = parse_workforce_detail(record, detail_html)
    except (RuntimeError, ValueError) as exc:
        raise ValueError('Surge role/application evidence is invalid.') from exc
    keys = ('external_id', 'url', 'title', 'location', 'department', 'expertise',
            'commitment', 'opportunity_kind', 'availability_basis',
            'include_in_live_market_estimate')
    if (any(getattr(parsed, key) != getattr(candidate, key) for key in keys)
            or normalize_source_body(parsed.source_body) != prepared.body):
        raise ValueError('Surge indexed role and captured record disagree.')


def _validate_handshake_public_cms_record_v1(
    attestation, candidate, prepared, context, *, provider, source_type,
):
    from wahojobs.classification import (
        AVAILABILITY_BASIS_PUBLIC_CMS, OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY,
    )
    from wahojobs.crawler.providers.handshake import (
        FIELD_ID, FIELD_SLUG, FIELD_SALARY, FIELD_APPLICATION, DETAIL_URL_PREFIX, _validate_asset_url,
        FIELD_SUBJECT_FILTERS, FIELD_DEGREE_FILTERS, SUBJECT_TITLE_FIELD, DEGREE_TITLE_FIELD,
        build_commitment,
        parse_opportunity_record, qualified_public_record,
    )
    evidence = json.loads(attestation.authority_evidence_json)
    metadata = json.loads(prepared.metadata_json)
    record = metadata.get('cms_record')
    required = {'cms_id', 'slug', 'title', 'application_url',
                'application_job_id', 'cms_chunk_url', 'cms_chunk_sha256'}
    metadata_keys = {'salary', 'subjects', 'degrees', 'application_url',
                     'cms_record', 'cms_chunk_url', 'cms_chunk_sha256', 'application_job_id',
                     'subject_label_evidence', 'degree_label_evidence'}
    subjects, degrees = metadata.get('subjects'), metadata.get('degrees')
    if (set(evidence) != required or set(metadata) != metadata_keys
            or type(subjects) is not list or type(degrees) is not list
            or any(type(value) is not str or not value for value in subjects + degrees)
            or type(record) is not dict
            or provider != 'handshake' or source_type != 'framer-public-inventory'
            or attestation.body_observation != BODY_OBSERVATION_PRESENT
            or prepared.body_format != 'text/plain' or not prepared.body
            or context.provider_outcome != ProviderOutcome.PARTIAL.value
            or context.used_sample_data or context.snapshot_complete or context.pagination_complete
            or context.crawl_run_id is None or context.candidate_count < 1
            or context.normalized_record_count != context.candidate_count
            or context.raw_record_count < context.candidate_count
            or context.payload_shape != HANDSHAKE_PUBLIC_CMS_RECORD_CONTRACT_ID
            or context.schema_fingerprint != HANDSHAKE_PUBLIC_CMS_RECORD_CONTRACT_ID
            or candidate.opportunity_kind != OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY
            or candidate.availability_basis != AVAILABILITY_BASIS_PUBLIC_CMS
            or candidate.include_in_live_market_estimate is not False
            or prepared.source_updated_at is not None
            or candidate.location != 'Remote'
            or evidence['cms_id'] != record.get(FIELD_ID)
            or candidate.external_id != 'handshake::' + str(evidence['cms_id'])
            or evidence['slug'] != record.get(FIELD_SLUG)
            or candidate.url != DETAIL_URL_PREFIX + '/' + str(evidence['slug'])
            or evidence['title'] != candidate.title
            or evidence['application_url'] != metadata.get('application_url')
            or evidence['application_url'] != record.get(FIELD_APPLICATION)
            or evidence['application_job_id'] != metadata.get('application_job_id')
            or evidence['cms_chunk_url'] != metadata.get('cms_chunk_url')
            or evidence['cms_chunk_sha256'] != metadata.get('cms_chunk_sha256')
            or metadata['salary'] != record.get(FIELD_SALARY)
            or re.fullmatch(r'[a-f0-9]{64}', str(evidence['cms_chunk_sha256'])) is None
            or not qualified_public_record(record)):
        raise ValueError('Handshake public CMS record authority is inconsistent.')
    _validate_asset_url(evidence['cms_chunk_url'])
    if '/cms/' not in evidence['cms_chunk_url']:
        raise ValueError('Handshake record must come from a CMS chunk.')
    from urllib.parse import parse_qs, urlsplit
    job_id = parse_qs(urlsplit(evidence['application_url']).query)['hai_job_id'][0]
    if evidence['application_job_id'] != job_id:
        raise ValueError('Handshake signup identity is inconsistent.')
    def validated_label_map(field, title_field, key):
        ids = record.get(field)
        ids = [] if ids is None else ids
        proofs = metadata.get(key)
        if (type(ids) is not list or type(proofs) is not dict
                or set(proofs) != set(ids) or len(ids) != len(set(ids))):
            raise ValueError('Handshake CMS facet references are inconsistent.')
        labels = {}
        for item_id, proof in proofs.items():
            if type(item_id) is not str or not item_id or type(proof) is not dict or set(proof) != {
                    'cms_record', 'cms_chunk_url', 'cms_chunk_sha256'}:
                raise ValueError('Handshake CMS facet evidence is invalid.')
            label_record = proof['cms_record']
            if (type(label_record) is not dict or label_record.get(FIELD_ID) != item_id
                    or type(label_record.get(title_field)) is not str
                    or not label_record[title_field].strip()
                    or type(proof['cms_chunk_url']) is not str
                    or re.fullmatch(r'[a-f0-9]{64}', str(proof['cms_chunk_sha256'])) is None):
                raise ValueError('Handshake CMS facet evidence disagrees with its identity.')
            _validate_asset_url(proof['cms_chunk_url'])
            if '/cms/' not in proof['cms_chunk_url']:
                raise ValueError('Handshake facet evidence is not a CMS chunk.')
            labels[item_id] = ' '.join(label_record[title_field].split())
        return labels
    subject_map = validated_label_map(FIELD_SUBJECT_FILTERS, SUBJECT_TITLE_FIELD,
                                      'subject_label_evidence')
    degree_map = validated_label_map(FIELD_DEGREE_FILTERS, DEGREE_TITLE_FIELD,
                                     'degree_label_evidence')
    parsed = parse_opportunity_record(record, subject_map, degree_map,
        subject_label_evidence=metadata['subject_label_evidence'],
        degree_label_evidence=metadata['degree_label_evidence'])
    fields = ('external_id', 'title', 'location', 'url', 'opportunity_kind',
              'availability_basis', 'include_in_live_market_estimate')
    if (any(getattr(parsed, field) != getattr(candidate, field) for field in fields)
            or normalize_source_body(parsed.source_body) != prepared.body
            or subjects != parsed.source_metadata['subjects']
            or degrees != parsed.source_metadata['degrees']
            or candidate.department != ('; '.join(subjects) if subjects else 'Unknown')
            or candidate.expertise != candidate.department
            or candidate.commitment != build_commitment(record.get(FIELD_SALARY), degrees)):
        raise ValueError('Handshake CMS record and candidate disagree.')


def _validate_outlier_index_detail_record_v1(
    attestation, candidate, prepared, context, *, provider, source_type,
):
    from wahojobs.crawler.providers.outlier import parse_index, qualify_index_detail, PUBLIC_PAGE_CHUNK_SHA256

    evidence = json.loads(attestation.authority_evidence_json)
    metadata = json.loads(prepared.metadata_json)
    index_payload = metadata.get('index_payload')
    row = metadata.get('index_row')
    detail = metadata.get('detail_record')
    if (set(evidence) != {'id', 'index_sha256', 'detail_sha256',
                         'signup_flow_id', 'public_url', 'public_client_sha256'}
            or set(metadata) != {'index_payload', 'index_row', 'detail_record',
                                 'application_action', 'signup_flow_id', 'allowed_countries',
                                 'source_location_label', 'location_display_contract',
                                 'public_client_sha256'}
            or type(index_payload) is not str or type(row) is not dict
            or type(detail) is not dict
            or provider != 'outlier' or source_type != 'outlier-job-board'
            or attestation.body_observation != BODY_OBSERVATION_PRESENT
            or prepared.body_format != 'text/html' or not prepared.body
            or prepared.source_updated_at is not None
            or context.provider_outcome != ProviderOutcome.PARTIAL.value
            or context.used_sample_data or context.snapshot_complete or context.pagination_complete
            or context.crawl_run_id is None or context.candidate_count < 1
            or context.normalized_record_count != context.candidate_count
            or context.raw_record_count < context.candidate_count
            or context.payload_shape != OUTLIER_INDEX_DETAIL_RECORD_CONTRACT_ID
            or context.schema_fingerprint != OUTLIER_INDEX_DETAIL_RECORD_CONTRACT_ID
            or candidate.include_in_live_market_estimate is not False
            or evidence['id'] != row.get('id')
            or evidence['index_sha256'] != hashlib.sha256(index_payload.encode()).hexdigest()
            or evidence['detail_sha256'] != hashlib.sha256(json.dumps(
                detail, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            or evidence['signup_flow_id'] != metadata['signup_flow_id']
            or evidence['public_client_sha256'] != PUBLIC_PAGE_CHUNK_SHA256
            or metadata['public_client_sha256'] != PUBLIC_PAGE_CHUNK_SHA256
            or evidence['public_url'] != candidate.url):
        raise ValueError('Outlier public record authority is inconsistent.')
    indexed = [item for item in parse_index(index_payload) if item.get('id') == evidence['id']]
    if len(indexed) != 1 or indexed[0] != row:
        raise ValueError('Outlier exact index row is not attested.')
    parsed = qualify_index_detail(row, detail, index_payload)
    fields = ('external_id', 'title', 'location', 'url', 'department', 'expertise',
              'opportunity_kind', 'availability_basis', 'include_in_live_market_estimate')
    if (any(getattr(parsed, key) != getattr(candidate, key) for key in fields)
            or normalize_source_body(parsed.source_body) != prepared.body
            or parsed.source_metadata != metadata
            or parsed.record_promotion_attestation.authority_evidence != evidence):
        raise ValueError('Outlier source record and candidate disagree.')


def _validate_provider_detail_content_v1(attestation, candidate, prepared, context, *, provider, source_type):
    from wahojobs.crawler.provider_details import DETAIL_KEY, validate_detail_url
    evidence = json.loads(attestation.authority_evidence_json)
    metadata = json.loads(prepared.metadata_json)
    detail = metadata.get(DETAIL_KEY, {})
    expected_type = {"alignerr": "alignerr-marketplace", "micro1": "micro1-marketplace", "mercor": "mercor-marketplace"}.get(provider)
    if (set(evidence) != {"url", "external_id", "response_sha256", "observed_at", "content_only"}
            or not expected_type or source_type != expected_type
            or evidence["content_only"] is not True
            or evidence["url"] != candidate.url or evidence["external_id"] != candidate.external_id
            or detail.get("url") != candidate.url or detail.get("external_id") != candidate.external_id
            or detail.get("provider") != provider or detail.get("version") != 1
            or detail.get("http_status") != 200 or not prepared.body
            or detail.get("response_sha256") != evidence["response_sha256"]
            or not re.fullmatch(r"[a-f0-9]{64}", str(evidence["response_sha256"]))
            or detail.get("observed_at") != evidence["observed_at"]
            or parse_source_timestamp(evidence["observed_at"])[0] != SOURCE_TIMESTAMP_VALID
            or context.crawl_run_id is not None or context.snapshot_complete or context.pagination_complete
            or context.empty_snapshot_validated or context.used_sample_data
            or context.provider_outcome != ProviderOutcome.PARTIAL.value
            or (context.raw_record_count, context.normalized_record_count, context.candidate_count,
                context.rejected_record_count) != (1, 1, 1, 0)
            or context.payload_shape != PROVIDER_DETAIL_RECORD_CONTRACT_ID
            or context.schema_fingerprint != PROVIDER_DETAIL_RECORD_CONTRACT_ID):
        raise ValueError("Provider detail content evidence is inconsistent")
    validate_detail_url(provider, candidate.external_id, candidate.url)
    record = detail.get("record", {})
    if provider == "alignerr":
        if record.get("id") != candidate.external_id or record.get("isActive") is not True:
            raise ValueError("Alignerr detail record identity/status is invalid")
        body = record.get("longDescription") or record.get("htmlLongDescription")
    elif provider == 'mercor':
        from wahojobs.crawler.providers.mercor import should_include_listing
        if (not should_include_listing(record) or record.get('listingId') != candidate.external_id
                or record.get('title') != candidate.title
                or detail.get('pay_evidence', {}).get('wording') != metadata.get('pay')):
            raise ValueError('Mercor pay detail identity/status is invalid')
        validate_detail_url(provider, candidate.external_id, detail.get('response_url', ''))
        body = record.get('description')
    else:
        if record.get("client_job_id") != candidate.external_id or record.get("job_status") != "open":
            raise ValueError("micro1 detail record identity/status is invalid")
        from html import escape
        body = record.get("job_description")
        questions = detail.get("screening_questions")
        if not isinstance(body, str) or not isinstance(questions, list):
            raise ValueError("micro1 detail content is incomplete")
        if questions:
            body += "<h2>Application screening questions</h2><ol>" + "".join(
                "<li>" + escape(q["question_text"]) + "</li>" for q in questions) + "</ol>"
    if normalize_source_body(body) != prepared.body:
        raise ValueError("Detail body differs from its source record")


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
    return _decide_source_material_promotion(
        prepared, accepted_row,
        same_accepted_semantic_material=same_accepted_semantic_material,
        authority_reasons=context.non_authoritative_reasons(),
    )


def _decide_source_material_promotion(
    prepared, accepted_row, *, same_accepted_semantic_material, authority_reasons,
):

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
    if record_attestation.contract_id in {
        DATAANNOTATION_CODING_RECORD_CONTRACT_ID, DATAANNOTATION_ROLE_RECORD_CONTRACT_ID,
        DATAFORCE_INDEX_DETAIL_RECORD_CONTRACT_ID,
        SURGE_REMOTE_WORKFORCE_RECORD_CONTRACT_ID,
        HANDSHAKE_PUBLIC_CMS_RECORD_CONTRACT_ID,
        OUTLIER_INDEX_DETAIL_RECORD_CONTRACT_ID,
    }:
        reasons = []
        if context.used_sample_data:
            reasons.append(REASON_SAMPLE_DATA)
        if context.provider_outcome not in {ProviderOutcome.SUCCESS.value, ProviderOutcome.PARTIAL.value}:
            reasons.append(REASON_PROVIDER_OUTCOME_NOT_SUCCESS)
        if context.normalized_record_count != context.candidate_count:
            reasons.append(REASON_RECORD_COUNT_MISMATCH)
        return _decide_source_material_promotion(
            prepared, accepted_row,
            same_accepted_semantic_material=same_accepted_semantic_material,
            authority_reasons=tuple(reasons),
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


def decide_mercor_record_promotion_v1(
    prepared, context, accepted_row, *, same_accepted_semantic_material=False,
    record_attestation=None, accepted_record_attestation=None,
):
    if record_attestation is None or record_attestation.contract_id != MERCOR_RECORD_CONTRACT_ID:
        raise ValueError("Mercor promotion requires validated record authority.")
    reasons = []
    if context.used_sample_data:
        reasons.append(REASON_SAMPLE_DATA)
    if context.provider_outcome not in {ProviderOutcome.SUCCESS.value, ProviderOutcome.PARTIAL.value}:
        reasons.append(REASON_PROVIDER_OUTCOME_NOT_SUCCESS)
    if context.normalized_record_count != context.candidate_count:
        reasons.append(REASON_RECORD_COUNT_MISMATCH)
    # Availability was directly observed even when the source supplies no content
    # timestamp. Preserve the existing material/timestamp conflict safeguards.
    return _decide_source_material_promotion(
        prepared, accepted_row,
        same_accepted_semantic_material=same_accepted_semantic_material,
        authority_reasons=tuple(reasons),
    )


def decide_mercor_record_promotion_v2(
    prepared, context, accepted_row, *, same_accepted_semantic_material=False,
    record_attestation=None, accepted_record_attestation=None,
    job_fields_json=None, accepted_job_fields_json=None,
):
    """Retain dated exact-page evidence when a compatible summary omits it.

    This is a content hold, not a new detail observation. Null/empty pay in the
    summary has no documented retraction authority. Any other changed supplied
    field, body or identity invalidates reuse and follows ordinary promotion.
    Historical V1 decisions remain replayable without this rule.
    """
    ordinary = decide_mercor_record_promotion_v1(prepared, context, accepted_row,
        same_accepted_semantic_material=same_accepted_semantic_material,
        record_attestation=record_attestation,
        accepted_record_attestation=accepted_record_attestation)
    compatible_timestamp = (ordinary.decision == PROMOTION_DECISION_HELD_SOURCE_CONFLICT
                            and ordinary.reasons == (REASON_SOURCE_TIMESTAMP_CONFLICT,))
    if (not (ordinary.accepted or compatible_timestamp) or accepted_row is None
            or accepted_record_attestation is None
            or accepted_record_attestation.contract_id not in (PROVIDER_DETAIL_RECORD_CONTRACT_ID, 'mercor_supplemental_composition_v1')
            or not job_fields_json or job_fields_json != accepted_job_fields_json):
        return ordinary
    from wahojobs.crawler.provider_details import DETAIL_KEY
    previous = json.loads(accepted_row['metadata_json'])
    current = json.loads(prepared.metadata_json)
    detail = previous.get(DETAIL_KEY, {})
    if (detail.get('provider') != 'mercor' or not detail.get('pay_evidence')
            or prepared.body != accepted_row['body']
            or prepared.body_format != accepted_row['body_format']):
        return ordinary
    # Summary payRate:null/empty means undisclosed in this envelope, not that
    # the exact page withdrew its advertised rate. Other explicit empties are
    # real field changes and must not be erased by a generic dictionary merge.
    compared = {k: v for k, v in current.items()
                if not (k in ('payRate', 'payRateFrequency') and v in (None, ''))}
    if (all(k in previous and previous[k] == v for k, v in compared.items())
            and 'pay' not in current and DETAIL_KEY not in current):
        return SourcePromotionDecision(PROMOTION_DECISION_HELD_DEGRADED,
            ('summary_omits_compatible_supplemental_content',),
            _accepted_timestamp(accepted_row))
    return ordinary


def decide_provider_detail_content_promotion_v1(
    prepared, context, accepted_row, *, same_accepted_semantic_material=False,
    record_attestation=None, accepted_record_attestation=None,
):
    if record_attestation is None or record_attestation.contract_id != PROVIDER_DETAIL_RECORD_CONTRACT_ID:
        if (accepted_record_attestation is None
                or accepted_record_attestation.contract_id != PROVIDER_DETAIL_RECORD_CONTRACT_ID):
            raise ValueError("Detail promotion requires validated content authority")
        return SourcePromotionDecision(PROMOTION_DECISION_HELD_DEGRADED,
                                       ("catalog_cannot_replace_accepted_detail",),
                                       _accepted_timestamp(accepted_row))
    # Authority is for this content only. It gives no removal or availability permission.
    return _decide_source_material_promotion(prepared, accepted_row,
        same_accepted_semantic_material=same_accepted_semantic_material, authority_reasons=())


def decide_mercor_detail_content_promotion_v2(prepared, context, accepted_row, **options):
    ordinary = decide_provider_detail_content_promotion_v1(prepared, context, accepted_row, **options)
    if (ordinary.decision != PROMOTION_DECISION_HELD_SOURCE_CONFLICT
            or ordinary.reasons != (REASON_SOURCE_TIMESTAMP_CONFLICT,) or accepted_row is None):
        return ordinary
    # Catalog updatedAt is not the clock for separately observed exact-page pay.
    # Keep the former while requiring a strictly later detail observation.
    from wahojobs.crawler.provider_details import DETAIL_KEY
    current = json.loads(prepared.metadata_json).get(DETAIL_KEY, {})
    previous = json.loads(accepted_row['metadata_json']).get(DETAIL_KEY, {})
    old_status, old_time = parse_source_timestamp(previous.get('observed_at'))
    new_status, new_time = parse_source_timestamp(current.get('observed_at'))
    capture_status, capture_time = parse_source_timestamp(accepted_row['last_captured_at'])
    if (current.get('provider') == 'mercor' and not previous
            and new_status == capture_status == SOURCE_TIMESTAMP_VALID
            and new_time >= capture_time and prepared.body == accepted_row['body']):
        return SourcePromotionDecision(PROMOTION_DECISION_PROMOTED,
            ('separately_dated_exact_detail_observation',), _accepted_timestamp(accepted_row))
    if (current.get('provider') == previous.get('provider') == 'mercor'
            and old_status == new_status == SOURCE_TIMESTAMP_VALID and new_time > old_time):
        return SourcePromotionDecision(PROMOTION_DECISION_PROMOTED,
            ('later_exact_detail_observation',), _accepted_timestamp(accepted_row))
    return ordinary


# Historical capture and policy implementations are permanently pinned.  A
# future current-version bump must add a new literal mapping rather than making
# old captures follow mutable current behavior.
from wahojobs.mercor_supplemental import CONTRACT as SUPPLEMENTAL_CONTRACT, validate as validate_supplemental, decide as decide_supplemental

SOURCE_CAPTURE_CONTRACT_PREPARERS = {
    "job_source_capture_v1": prepare_source_capture_v1,
}
RECORD_PROMOTION_CONTRACT_VALIDATORS = {
    DATAANNOTATION_CODING_RECORD_CONTRACT_ID: _validate_dataannotation_coding_record_v1,
    DATAANNOTATION_ROLE_RECORD_CONTRACT_ID: _validate_dataannotation_role_record_v2,
    DATAFORCE_INDEX_DETAIL_RECORD_CONTRACT_ID: _validate_dataforce_index_detail_record_v1,
    SURGE_REMOTE_WORKFORCE_RECORD_CONTRACT_ID: _validate_surge_remote_workforce_record_v1,
    HANDSHAKE_PUBLIC_CMS_RECORD_CONTRACT_ID: _validate_handshake_public_cms_record_v1,
    OUTLIER_INDEX_DETAIL_RECORD_CONTRACT_ID: _validate_outlier_index_detail_record_v1,
    SUPPLEMENTAL_CONTRACT: validate_supplemental,
    "meridial_greenhouse_record_v1": _validate_meridial_greenhouse_record_v1,
    "mercor_public_active_record_v1": _validate_mercor_public_active_record_v1,
    "provider_detail_content_v1": _validate_provider_detail_content_v1,
}
SOURCE_PROMOTION_POLICY_DECIDERS = {
    SUPPLEMENTAL_CONTRACT: decide_supplemental,
    "job_source_promotion_v1": decide_source_promotion_v1,
    "job_source_promotion_v2": decide_source_promotion_v2,
    "mercor_record_promotion_v1": decide_mercor_record_promotion_v1,
    "mercor_record_promotion_v2": decide_mercor_record_promotion_v2,
    "provider_detail_content_promotion_v1": decide_provider_detail_content_promotion_v1,
    "mercor_detail_content_promotion_v2": decide_mercor_detail_content_promotion_v2,
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

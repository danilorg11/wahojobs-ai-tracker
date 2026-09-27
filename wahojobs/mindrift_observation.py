"""Versioned positive Workable evidence, independent of absence authority."""
from dataclasses import replace
import json
import re

from wahojobs.crawler.types import (
    BODY_OBSERVATION_NOT_OBSERVED, BODY_OBSERVATION_PRESENT,
    RecordPromotionAttestation,
)

CONTRACT = "mindrift_workable_public_record_v1"
SHAPE = "mindrift-workable:published-records:v1"
ENDPOINT = "https://apply.workable.com/api/v3/accounts/toloka-ai/jobs"
ACCOUNT = "toloka-ai"
COUNT_DROP_WARNING = "mindrift_count_drop_individual_only"


def attest(candidate, row):
    """Called only after the full, unique-shortcode pagination has validated."""
    if (row.get("state") != "published" or row.get("isInternal") is not False
            or not isinstance(row.get("shortcode"), str)
            or re.fullmatch(r"[A-Za-z0-9_-]+", row["shortcode"]) is None):
        raise ValueError("Mindrift public record has no explicit public identity.")
    return replace(candidate, record_promotion_attestation=RecordPromotionAttestation(
        contract_id=CONTRACT,
        body_observation=BODY_OBSERVATION_PRESENT if candidate.source_body else BODY_OBSERVATION_NOT_OBSERVED,
        authority_evidence={"endpoint": ENDPOINT, "account": ACCOUNT, "record": row},
    ))


def validate(attestation, candidate, prepared, context, *, provider, source_type):
    from wahojobs.crawler.providers.workable_markdown import parse_workable_row
    from wahojobs.source_capture import prepare_source_capture_v1, SEMANTIC_JOB_FIELD_NAMES
    evidence = json.loads(attestation.authority_evidence_json)
    if (set(evidence) != {"endpoint", "account", "record"}
            or evidence["endpoint"] != ENDPOINT or evidence["account"] != ACCOUNT
            or provider != "mindrift" or source_type != "workable-careers-api"
            or context.payload_shape != SHAPE or context.schema_fingerprint != CONTRACT
            or context.provider_outcome not in {"success", "partial"}
            or context.used_sample_data or not context.pagination_complete
            or context.crawl_run_id is None or context.candidate_count < 1
            or context.normalized_record_count != context.candidate_count
            or context.raw_record_count != context.normalized_record_count
            or context.rejected_record_count != 0
            or type(evidence["record"]) is not dict):
        raise ValueError("Mindrift public record authority is inconsistent.")
    raw = evidence["record"]
    expected = parse_workable_row(ACCOUNT, raw)
    if expected is None:
        raise ValueError("Mindrift record is not a public published observation.")
    # Rebuild every projected field from retained upstream data. The tracker may
    # add only its stable source hash; neither identity nor content is inferred.
    expected_attestation = attest(expected, raw).record_promotion_attestation
    if (any(getattr(candidate, field) != getattr(expected, field)
            for field in SEMANTIC_JOB_FIELD_NAMES if field != "source_hash")
            or prepared != prepare_source_capture_v1(expected)
            or attestation.body_observation != expected_attestation.body_observation):
        raise ValueError("Mindrift record and normalized candidate disagree.")

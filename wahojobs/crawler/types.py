from dataclasses import dataclass
from enum import Enum


class ProviderOutcome(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    CONTRACT_DRIFT = "contract_drift"


REMOVAL_SKIP_OUTCOME = "provider outcome is not success"
REMOVAL_SKIP_SNAPSHOT = "snapshot is not complete"
REMOVAL_SKIP_PAGINATION = "pagination is not complete"
REMOVAL_SKIP_SAMPLE = "sample data cannot authorize removals"
REMOVAL_SKIP_EMPTY = "empty snapshot was not explicitly validated"
REMOVAL_SKIP_COUNT_MISMATCH = "normalized record count does not match job records"
LEGACY_CONTRACT_WARNING = (
    "Provider has not declared an authoritative snapshot contract; removals skipped."
)


@dataclass(frozen=True)
class JobCandidate:
    title: str
    location: str
    url: str
    external_id: str | None = None
    department: str | None = None
    expertise: str | None = None
    commitment: str | None = None
    opportunity_kind: str | None = None
    availability_basis: str | None = None
    include_in_live_market_estimate: bool | None = None
    source_hash: str = ""


@dataclass(frozen=True)
class CompanyCrawlResult:
    jobs: list[JobCandidate]
    used_sample_data: bool
    source_message: str
    source_type: str
    outcome: ProviderOutcome = ProviderOutcome.PARTIAL
    snapshot_complete: bool = False
    pagination_complete: bool = False
    empty_snapshot_validated: bool = False
    payload_shape: str = ""
    raw_record_count: int = 0
    normalized_record_count: int = 0
    rejected_record_count: int = 0
    warnings: tuple[str, ...] = ()
    schema_fingerprint: str = ""

    def __post_init__(self):
        object.__setattr__(self, "outcome", ProviderOutcome(self.outcome))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        for field_name in (
            "raw_record_count",
            "normalized_record_count",
            "rejected_record_count",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} cannot be negative.")


@dataclass(frozen=True)
class RemovalAuthorization:
    authorized: bool
    skip_reasons: tuple[str, ...]


def evaluate_removal_authorization(
    crawl_result: CompanyCrawlResult,
    safety_rejections: tuple[str, ...] = (),
) -> RemovalAuthorization:
    reasons = []
    if crawl_result.outcome != ProviderOutcome.SUCCESS:
        reasons.append(REMOVAL_SKIP_OUTCOME)
    if not crawl_result.snapshot_complete:
        reasons.append(REMOVAL_SKIP_SNAPSHOT)
    if not crawl_result.pagination_complete:
        reasons.append(REMOVAL_SKIP_PAGINATION)
    if crawl_result.used_sample_data:
        reasons.append(REMOVAL_SKIP_SAMPLE)
    if crawl_result.normalized_record_count != len(crawl_result.jobs):
        reasons.append(REMOVAL_SKIP_COUNT_MISMATCH)
    if (
        crawl_result.normalized_record_count == 0
        and not crawl_result.empty_snapshot_validated
    ):
        reasons.append(REMOVAL_SKIP_EMPTY)
    reasons.extend(reason for reason in safety_rejections if reason)
    return RemovalAuthorization(not reasons, tuple(reasons))


def crawl_run_status_for_result(
    crawl_result: CompanyCrawlResult,
    removal_authorization: RemovalAuthorization,
) -> str:
    if crawl_result.outcome == ProviderOutcome.CONTRACT_DRIFT:
        return "contract_drift"
    if crawl_result.used_sample_data:
        return "success"
    if (
        crawl_result.outcome == ProviderOutcome.SUCCESS
        and removal_authorization.authorized
    ):
        return "success"
    return "partial"


@dataclass(frozen=True)
class TrackingSummary:
    source_type: str
    jobs_found: int
    jobs_new: int
    jobs_reactivated: int
    jobs_updated: int
    jobs_removed: int
    active_jobs_total: int
    used_sample_data: bool
    source_message: str
    provider_outcome: ProviderOutcome = ProviderOutcome.PARTIAL
    snapshot_complete: bool = False
    pagination_complete: bool = False
    removals_authorized: bool = False
    removal_skip_reasons: tuple[str, ...] = ()
    raw_record_count: int = 0
    normalized_record_count: int = 0
    rejected_record_count: int = 0
    warnings: tuple[str, ...] = ()
    payload_shape: str = ""
    schema_fingerprint: str = ""

    def __post_init__(self):
        object.__setattr__(self, "provider_outcome", ProviderOutcome(self.provider_outcome))
        object.__setattr__(self, "removal_skip_reasons", tuple(self.removal_skip_reasons))
        object.__setattr__(self, "warnings", tuple(self.warnings))

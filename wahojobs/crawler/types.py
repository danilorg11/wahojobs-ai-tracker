from dataclasses import dataclass


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

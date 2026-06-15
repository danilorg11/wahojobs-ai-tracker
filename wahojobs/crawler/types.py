from dataclasses import dataclass


@dataclass(frozen=True)
class JobCandidate:
    title: str
    location: str
    url: str
    external_id: str | None = None
    source_hash: str = ""


@dataclass(frozen=True)
class CompanyCrawlResult:
    jobs: list[JobCandidate]
    used_sample_data: bool
    source_message: str


@dataclass(frozen=True)
class TrackingSummary:
    jobs_found: int
    jobs_new: int
    jobs_reactivated: int
    jobs_updated: int
    jobs_removed: int
    active_jobs_total: int
    used_sample_data: bool
    source_message: str


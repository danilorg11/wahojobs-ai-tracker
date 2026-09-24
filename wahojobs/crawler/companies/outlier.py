from wahojobs.crawler.providers.outlier import fetch_outlier_jobs
from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome

OUTLIER_API_URL = "https://app.outlier.ai/internal/experts/job-board/jobs"


def crawl_outlier(api_url):
    if api_url != OUTLIER_API_URL:
        raise ValueError("Outlier endpoint differs from the reviewed public board endpoint.")
    jobs = fetch_outlier_jobs(api_url)
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="outlier-job-board",
        source_message=f"Observed individually qualified public Outlier records: {api_url}",
        outcome=ProviderOutcome.PARTIAL,
        snapshot_complete=False,
        pagination_complete=False,
        raw_record_count=len(jobs),
        normalized_record_count=len(jobs),
    )

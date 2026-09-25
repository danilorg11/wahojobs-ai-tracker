from wahojobs.crawler.providers.outlier import fetch_outlier_jobs
from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome

OUTLIER_API_URL = "https://app.outlier.ai/internal/experts/job-board/jobs"


def crawl_outlier(api_url):
    if api_url != OUTLIER_API_URL:
        raise ValueError("Outlier endpoint differs from the reviewed public board endpoint.")
    jobs, raw_count, inspected_count = fetch_outlier_jobs(api_url)
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="outlier-job-board",
        source_message=(f"Outlier public board rows={raw_count}; inspected details={inspected_count}; "
                        f"individually qualified={len(jobs)}; no absence authority."),
        outcome=ProviderOutcome.PARTIAL if jobs else ProviderOutcome.ANOMALOUS,
        snapshot_complete=False,
        pagination_complete=False,
        raw_record_count=raw_count,
        normalized_record_count=len(jobs),
        filtered_record_count=raw_count-len(jobs),
        payload_shape="outlier_index_detail_record_v1",
        schema_fingerprint="outlier_index_detail_record_v1",
    )

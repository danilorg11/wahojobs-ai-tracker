from wahojobs.crawler.providers.outlier import fetch_outlier_jobs
from wahojobs.crawler.types import CompanyCrawlResult, JobCandidate


OUTLIER_API_URL = "https://app.outlier.ai/internal/experts/job-board/jobs"


SAMPLE_JOBS = [
    JobCandidate(
        title="AI Trainer - Coding Expertise",
        location="Remote",
        url="https://outlier.ai/sample/ai-trainer-coding",
        external_id="sample-outlier-coding",
    ),
    JobCandidate(
        title="AI Trainer - Mathematics",
        location="Remote",
        url="https://outlier.ai/sample/ai-trainer-math",
        external_id="sample-outlier-math",
    ),
    JobCandidate(
        title="AI Writing Evaluator",
        location="Remote",
        url="https://outlier.ai/sample/ai-writing-evaluator",
        external_id="sample-outlier-writing",
    ),
]


def crawl_outlier(api_url):
    source_url = api_url if "internal/experts/job-board/jobs" in api_url else OUTLIER_API_URL
    try:
        jobs = fetch_outlier_jobs(source_url)
        if jobs:
            return CompanyCrawlResult(
                jobs=jobs,
                used_sample_data=False,
                source_type="outlier-job-board",
                source_message=f"Fetched live Outlier opportunities from API: {source_url}",
            )
        error_message = "Outlier API returned zero usable jobs."
    except Exception as exc:
        error_message = str(exc)

    return CompanyCrawlResult(
        jobs=SAMPLE_JOBS,
        used_sample_data=True,
        source_type="sample",
        source_message=(
            "Using SAMPLE DATA because live Outlier opportunities could not be fetched. "
            + error_message
        ),
    )

from wahojobs.crawler.providers.handshake import fetch_handshake_jobs
from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome


def crawl_handshake(opportunities_url):
    observed, raw_count, rejected_count = fetch_handshake_jobs(opportunities_url)
    return CompanyCrawlResult(
        # CMS visibility alone has no accepted per-record promotion contract.
        # Keep observed candidates out of lifecycle until that authority exists.
        jobs=[],
        used_sample_data=False,
        source_message=(
            "Fetched public Handshake AI opportunities from Framer CMS-backed "
            f"inventory: {opportunities_url}; {len(observed)} remote records held "
            "without individual promotion authority"
        ),
        source_type="framer-public-inventory",
        outcome=ProviderOutcome.PARTIAL,
        snapshot_complete=False,
        pagination_complete=False,
        raw_record_count=raw_count,
        normalized_record_count=0,
        rejected_record_count=rejected_count,
        filtered_record_count=raw_count-rejected_count,
    )

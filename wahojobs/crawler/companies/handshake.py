from wahojobs.crawler.providers.handshake import fetch_handshake_jobs
from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome


def crawl_handshake(opportunities_url):
    observed, raw_count, rejected_count = fetch_handshake_jobs(opportunities_url)
    qualified = [job for job in observed if job.record_promotion_attestation is not None]
    return CompanyCrawlResult(
        jobs=qualified,
        used_sample_data=False,
        source_message=(
            "Fetched public Handshake AI opportunities from Framer CMS-backed "
            f"inventory: {opportunities_url}; {len(qualified)} individually attested remote records"
        ),
        source_type="framer-public-inventory",
        outcome=ProviderOutcome.PARTIAL,
        snapshot_complete=False,
        pagination_complete=False,
        raw_record_count=raw_count,
        normalized_record_count=len(qualified),
        rejected_record_count=rejected_count,
        filtered_record_count=raw_count-rejected_count-len(qualified),
        payload_shape='handshake_public_cms_record_v1',
        schema_fingerprint='handshake_public_cms_record_v1',
    )

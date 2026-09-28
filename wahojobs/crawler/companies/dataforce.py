from wahojobs.crawler.providers.dataforce import (fetch_dataforce_jobs, collect_index_linked_details,
    supported_thyme_family, QUALIFIED_DETAIL_PATHS)
from urllib.parse import urlparse
from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome
from wahojobs.daily_source_policy import current_source


def crawl_dataforce(projects_url):
    observed = fetch_dataforce_jobs(projects_url)
    qualified, detail_count, inspection_failures, verification_failures = (
        collect_index_linked_details(observed)
        if current_source() == 'dataforce' else ([], 0, 0, 0))
    in_scope = sum(urlparse(job.url).path in QUALIFIED_DETAIL_PATHS or supported_thyme_family(job)
                   for job in observed)
    # Index cards expose a preview URL, not a verified application action.
    # Keep them as source observations until a source-specific record contract
    # can attest application authority; passing them to tracking can reactivate
    # old accepted variants before a held capture is considered.
    return CompanyCrawlResult(
        jobs=qualified,
        used_sample_data=False,
        source_type="dataforce-community-html",
        source_message=(f"Observed DataForce index rows and requested {detail_count} exact linked pages; "
                        f"qualified {len(qualified)} individually attested roles; "
                        f"known-role verification failures={verification_failures}; "
                        f"exploratory inspection failures={inspection_failures}: {projects_url}"),
        outcome=ProviderOutcome.PARTIAL,
        snapshot_complete=False,
        pagination_complete=False,
        raw_record_count=len(observed),
        normalized_record_count=len(qualified),
        filtered_record_count=len(observed)-len(qualified),
        warnings=((f'dataforce_in_scope_records_unqualified:{in_scope-len(qualified)}',)
                  if in_scope > len(qualified) else ()),
        payload_shape='dataforce_index_detail_record_v1',
        schema_fingerprint='dataforce_index_detail_record_v1',
    )

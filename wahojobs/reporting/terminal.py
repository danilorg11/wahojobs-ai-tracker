from wahojobs.db.connection import get_connection
from wahojobs.db.repository import get_last_successful_crawl


def print_crawl_summary(company, summary):
    print("")
    print("Wahojobs Phase 1 Crawl Summary")
    print("=" * 34)
    print(f"Company: {company['name']}")
    print(f"Source: {summary.source_message}")
    if summary.used_sample_data:
        print("Data mode: SAMPLE DATA - live Outlier jobs were not available.")
    else:
        print("Data mode: LIVE")
    print("")
    print(f"Jobs found:       {summary.jobs_found}")
    print(f"New jobs:         {summary.jobs_new}")
    print(f"Reactivated jobs: {summary.jobs_reactivated}")
    print(f"Updated jobs:     {summary.jobs_updated}")
    print(f"Removed jobs:     {summary.jobs_removed}")
    print(f"Active jobs now:  {summary.active_jobs_total}")

    with get_connection() as conn:
        last_run = get_last_successful_crawl(conn, company["id"])
        if last_run:
            print(f"Last crawl:       {last_run['finished_at']}")
    print("")


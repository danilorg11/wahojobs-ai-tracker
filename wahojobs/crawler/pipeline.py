from datetime import datetime, timezone

from wahojobs.crawler.companies.appen import crawl_appen
from wahojobs.crawler.companies.invisible import crawl_invisible
from wahojobs.crawler.companies.meridial import crawl_meridial
from wahojobs.crawler.companies.outlier import crawl_outlier
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import (
    create_crawl_run,
    fail_crawl_run,
    finish_crawl_run,
    get_company_by_slug,
)
from wahojobs.tracking.service import track_crawl_result


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


CRAWLERS = {
    "appen": crawl_appen,
    "invisible": crawl_invisible,
    "meridial": crawl_meridial,
    "outlier": crawl_outlier,
}


def run_crawl(company_slug="appen"):
    with get_connection() as conn:
        company = get_company_by_slug(conn, company_slug)
        if company is None:
            raise RuntimeError(
                f"Company '{company_slug}' is not configured. Run scripts/init_db.py first."
            )

        started_at = utc_now()
        crawl_run_id = create_crawl_run(conn, company["id"], started_at)
        conn.commit()

        try:
            crawler = CRAWLERS.get(company_slug)
            if crawler is None:
                raise ValueError(f"No crawler is implemented for '{company_slug}'.")

            crawl_result = crawler(company["careers_url"])
            summary = track_crawl_result(conn, company["id"], crawl_result, utc_now())
            finish_crawl_run(conn, crawl_run_id, summary, utc_now())
            conn.commit()
            return company, summary
        except Exception as exc:
            fail_crawl_run(conn, crawl_run_id, str(exc), utc_now())
            conn.commit()
            raise

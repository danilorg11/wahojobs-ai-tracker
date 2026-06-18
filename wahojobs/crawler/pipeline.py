from datetime import datetime, timezone

from wahojobs.crawler.companies.alignerr import crawl_alignerr
from wahojobs.crawler.companies.appen import crawl_appen
from wahojobs.crawler.companies.dataforce import crawl_dataforce
from wahojobs.crawler.companies.invisible import crawl_invisible
from wahojobs.crawler.companies.meridial import crawl_meridial
from wahojobs.crawler.companies.mercor import crawl_mercor
from wahojobs.crawler.companies.micro1 import crawl_micro1
from wahojobs.crawler.companies.mindrift import crawl_mindrift
from wahojobs.crawler.companies.oneforma import crawl_oneforma
from wahojobs.crawler.companies.outlier import crawl_outlier
from wahojobs.crawler.companies.rws import crawl_rws
from wahojobs.crawler.companies.turing import crawl_turing
from wahojobs.crawler.companies.welocalize import crawl_welocalize
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
    "alignerr": crawl_alignerr,
    "appen": crawl_appen,
    "dataforce": crawl_dataforce,
    "invisible": crawl_invisible,
    "meridial": crawl_meridial,
    "mercor": crawl_mercor,
    "micro1": crawl_micro1,
    "mindrift": crawl_mindrift,
    "oneforma": crawl_oneforma,
    "outlier": crawl_outlier,
    "rws": crawl_rws,
    "turing": crawl_turing,
    "welocalize": crawl_welocalize,
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
            summary = track_crawl_result(
                conn,
                company["id"],
                crawl_run_id,
                crawl_result,
                utc_now(),
            )
            finish_crawl_run(conn, crawl_run_id, summary, utc_now())
            conn.commit()
            return company, summary
        except Exception as exc:
            fail_crawl_run(conn, crawl_run_id, str(exc), utc_now())
            conn.commit()
            raise

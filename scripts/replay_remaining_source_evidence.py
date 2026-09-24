"""Offline DA/DF qualification replay of an already retained controlled batch.

This reads exact journal responses and writes no production database. A stopped
DataAnnotation collection remains stopped; its coding page is replayed as one
separately qualified, partial-source observation, never as a complete run.
"""

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from wahojobs.crawler.companies.dataforce import crawl_dataforce
from wahojobs.crawler.providers.dataannotation import DOMAIN_PAGES, parse_domain_page
from wahojobs.crawler.providers.dataforce import (
    parse_jobs_page, validate_inventory_page, validate_pagination,
)
from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import create_crawl_run, install_base_schema
from wahojobs.tracking.service import track_crawl_result


DA_PLAN = "21b5cb116428a556338c15430aa0a5b6cddc4b2e4368952f28a950fb1d41d010"
DF_PLAN = "716598fc7e215db5035650f9ef2739661afbe9609606ea27f63af962e4552dd2"


def response(journal, plan, sequence):
    root = journal / plan
    event = json.loads((root / f"{sequence:06d}.json").read_text(encoding="utf-8"))["data"]
    raw = (root / event["raw_response"]["file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != event["raw_response"]["sha256"]:
        raise ValueError("retained response checksum changed")
    if event["status"] != 200 or event["final_url"] != event["url"]:
        raise ValueError("retained 200 response or final URL changed")
    return raw.decode("utf-8"), event


def redirect(journal, plan, sequence, requested, location):
    event = json.loads((journal / plan / f"{sequence:06d}.json").read_text(encoding="utf-8"))["data"]
    raw = (journal / plan / event["raw_response"]["file"]).read_bytes()
    if (hashlib.sha256(raw).hexdigest() != event["raw_response"]["sha256"]
            or event["status"] != 301 or event["url"] != requested
            or event["response_headers"].get("Location") != location):
        raise ValueError("retained redirect evidence changed")
    return event


def replay(root):
    journal = root / "journal"
    redirect(journal, DA_PLAN, 3, "https://www.dataannotation.tech/coding",
             "/job-board/software-engineer")
    da_html, da_event = response(journal, DA_PLAN, 6)
    if da_event["url"] != "https://www.dataannotation.tech/job-board/software-engineer":
        raise ValueError("DataAnnotation coding destination changed")
    coding = parse_domain_page(DOMAIN_PAGES[0], da_event["url"], da_html)
    redirect(journal, DA_PLAN, 8, "https://www.dataannotation.tech/generalist",
             "/job-board/generalist")
    da = CompanyCrawlResult(
        jobs=[coding], used_sample_data=False,
        source_message="offline coding page derived from stopped controlled batch",
        source_type="evergreen-application-pages", outcome=ProviderOutcome.PARTIAL,
        raw_record_count=1, normalized_record_count=1,
        payload_shape="dataannotation_coding_evergreen_record_v1",
        schema_fingerprint="dataannotation_coding_evergreen_record_v1",
    )
    df_rows = []
    df_observed_at = None
    for page, sequence in enumerate((3, 5)):
        body, event = response(journal, DF_PLAN, sequence)
        expected = "https://dataforcecommunity.transperfect.com/projects"
        if page:
            expected += "?project_type=All&page=1"
        if event["url"] != expected:
            raise ValueError("DataForce request sequence changed")
        validate_inventory_page(body)
        has_next = validate_pagination(body, page)
        if has_next != (page == 0):
            raise ValueError("DataForce terminal pager changed")
        df_rows.extend(parse_jobs_page(body, event["url"]))
        df_observed_at = event["observed_at"]
    out_of_range, event = response(journal, DF_PLAN, 7)
    if event["url"] != "https://dataforcecommunity.transperfect.com/projects?project_type=All&page=2":
        raise ValueError("DataForce page-2 request changed")
    validate_inventory_page(out_of_range)
    if (parse_jobs_page(out_of_range, event["url"])
            or '<div class="view-empty">' not in out_of_range
            or "At the moment we don't have any active projects for this location." not in out_of_range):
        raise ValueError("DataForce page-2 empty view changed")
    try:
        validate_pagination(out_of_range, 2)
    except ValueError:
        pass
    else:
        raise ValueError("DataForce out-of-range page unexpectedly qualified")
    if len(df_rows) != len({job.external_id for job in df_rows}):
        raise ValueError("DataForce duplicate project identity")
    with tempfile.TemporaryDirectory() as directory:
        conn = get_connection(Path(directory) / "isolated-replay.sqlite")
        try:
            install_base_schema(conn)
            company_id = conn.execute(
                "INSERT INTO companies(name,slug,careers_url,source_tier,inventory_model,market_count_policy) "
                "VALUES('DataAnnotation','dataannotation','https://www.dataannotation.tech',"
                "'core','evergreen_application','exclude_from_live_count')"
            ).lastrowid
            observed_at = da_event["observed_at"]
            run_id = create_crawl_run(conn, company_id, observed_at)
            summary = track_crawl_result(conn, company_id, run_id, da, observed_at, model_enrichment=False)
            captures = [dict(row) for row in conn.execute(
                "SELECT record_promotion_contract_id,promotion_decision FROM job_source_content_captures")]
            variants = [dict(row) for row in conn.execute(
                "SELECT external_id,semantic_authority_state FROM jobs")]
            canonicals = [dict(row) for row in conn.execute(
                "SELECT canonical_key,canonical_title FROM canonical_opportunities")]
            df_company_id = conn.execute(
                "INSERT INTO companies(name,slug,careers_url,source_tier,inventory_model,market_count_policy) "
                "VALUES('DataForce','dataforce','https://dataforcecommunity.transperfect.com/projects',"
                "'core','live_feed','count_live')"
            ).lastrowid
            with patch("wahojobs.crawler.companies.dataforce.fetch_dataforce_jobs", return_value=df_rows):
                df_result = crawl_dataforce("https://dataforcecommunity.transperfect.com/projects")
            df_run = create_crawl_run(conn, df_company_id, df_observed_at)
            df_summary = track_crawl_result(conn, df_company_id, df_run, df_result,
                                            df_observed_at, model_enrichment=False)
            df_catalog_count = conn.execute(
                "SELECT COUNT(*) FROM canonical_opportunities WHERE company_id=?",
                (df_company_id,),
            ).fetchone()[0]
        finally:
            conn.close()
    return {
        "dataannotation": {"collection_status": "failed_at_unapproved_generalist_redirect",
                           "derived_observation": "coding_only_partial",
                           "observed_at": da_event["observed_at"], "raw_sha256": da_event["raw_response"]["sha256"],
                           "removals_authorized": summary.removals_authorized,
                           "captures": captures, "variants": variants, "canonicals": canonicals},
        "dataforce": {"collection_status": "partial", "observed_rows": len(df_rows),
                      "unique_ids": len({job.external_id for job in df_rows}),
                      "terminal": "page_1_pager_no_next", "page_2": "out_of_range_empty_not_inventory_empty",
                      "application_verified_rows": 0, "eligible_for_publication": 0,
                      "lifecycle_jobs_new": df_summary.jobs_new,
                      "lifecycle_removals_authorized": df_summary.removals_authorized,
                      "catalog_opportunities": df_catalog_count},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.evidence_root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

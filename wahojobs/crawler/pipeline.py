from datetime import datetime, timezone
from dataclasses import replace

from wahojobs.crawler.companies.alignerr import crawl_alignerr
from wahojobs.crawler.companies.appen import crawl_appen
from wahojobs.crawler.companies.dataannotation import crawl_dataannotation
from wahojobs.crawler.companies.dataforce import crawl_dataforce
from wahojobs.crawler.companies.handshake import crawl_handshake
from wahojobs.crawler.companies.invisible import crawl_invisible
from wahojobs.crawler.companies.meridial import crawl_meridial
from wahojobs.crawler.companies.mercor import crawl_mercor
from wahojobs.crawler.companies.micro1 import crawl_micro1
from wahojobs.crawler.companies.mindrift import crawl_mindrift
from wahojobs.crawler.companies.oneforma import crawl_oneforma
from wahojobs.crawler.companies.outlier import crawl_outlier
from wahojobs.crawler.companies.rws import crawl_rws
from wahojobs.crawler.companies.surge import crawl_surge
from wahojobs.crawler.companies.turing import crawl_turing
from wahojobs.crawler.companies.welocalize import crawl_welocalize
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import (
    create_crawl_run,
    fail_crawl_run,
    finish_crawl_run,
    get_company_by_slug,
)
from wahojobs.crawler.types import (
    ProviderOutcome,
    crawl_run_status_for_result,
    evaluate_removal_authorization,
)
from wahojobs.crawler.source_registry import assert_production_dispatch_allowed
from wahojobs.tracking.service import (
    summarize_crawl_result_without_lifecycle,
    track_crawl_result,
)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


CRAWLERS = {
    "alignerr": crawl_alignerr,
    "appen": crawl_appen,
    "dataannotation": crawl_dataannotation,
    "dataforce": crawl_dataforce,
    "handshake": crawl_handshake,
    "invisible": crawl_invisible,
    "meridial": crawl_meridial,
    "mercor": crawl_mercor,
    "micro1": crawl_micro1,
    "mindrift": crawl_mindrift,
    "oneforma": crawl_oneforma,
    "outlier": crawl_outlier,
    "rws": crawl_rws,
    "surge": crawl_surge,
    "turing": crawl_turing,
    "welocalize": crawl_welocalize,
}


def run_crawl(company_slug="appen", *, db_path=None, details=None, ownership=None, observation=None,
              authorize_controlled_publication=False):
    if ownership is not None and db_path is None:
        raise ValueError("Owned crawl requires an explicit local database")
    if details not in (None, "needed", "all"):
        raise ValueError("Unknown detail recovery mode")
    if details and db_path is None:
        raise ValueError("Detail recovery requires an explicit local database")
    registry_entry = assert_production_dispatch_allowed(company_slug)
    if observation is not None:
        from wahojobs.crawler.staged_observation import validate_observation
        if db_path is None or ownership is None or details is not None:
            raise ValueError('staged_publication_requires_owned_catalog_only')
        validate_observation(observation, company_slug)
        if observation.controlled_validation and not (authorize_controlled_publication
                and company_slug in ('dataannotation', 'dataforce')):
            raise ValueError('controlled_validation_requires_explicit_publication_authorization')
    from wahojobs.crawler.local_inventory import local_inventory_connection
    connection = get_connection() if db_path is None else local_inventory_connection(db_path, ownership=ownership)
    with connection as conn:
        company = get_company_by_slug(conn, company_slug)
        if company is None:
            preparation = "Initialize this selected development inventory first." if db_path is not None else "Run scripts/init_db.py first."
            raise RuntimeError(
                f"Company '{company_slug}' is not configured. {preparation}"
            )

        if observation is not None and observation.careers_url != company['careers_url']:
            raise ValueError('staged_source_configuration_changed')
        started_at = observation.started_at if observation is not None else utc_now()
        crawl_run_id = create_crawl_run(conn, company["id"], started_at)
        conn.commit()

        savepoint_name = None
        savepoint_active = False
        try:
            crawler = CRAWLERS.get(company_slug)
            if crawler is None:
                raise ValueError(f"No crawler is implemented for '{company_slug}'.")

            crawl_result = observation.result if observation is not None else crawler(company["careers_url"])
            if registry_entry is not None and registry_entry.ats_provider == "greenhouse":
                from wahojobs.crawler.greenhouse_pilot import apply_count_drop_policy

                crawl_result = apply_count_drop_policy(
                    registry_entry,
                    crawl_result,
                    previous_accepted_count=last_accepted_snapshot_count(
                        conn, company["id"]
                    ),
                )
            from wahojobs.daily_source_policy import current_source
            if current_source() is not None and crawl_result.used_sample_data:
                raise ValueError("daily_synthetic_evidence_forbidden")
            removal_authorization = evaluate_removal_authorization(crawl_result)

            if crawl_result.outcome == ProviderOutcome.CONTRACT_DRIFT:
                summary = summarize_crawl_result_without_lifecycle(
                    conn,
                    company["id"],
                    crawl_result,
                )
            else:
                conn.execute("BEGIN")
                savepoint_name = f"crawl_lifecycle_{int(crawl_run_id)}"
                conn.execute(f"SAVEPOINT {savepoint_name}")
                savepoint_active = True
                tracking_options = {} if db_path is None else {"model_enrichment": False}
                summary = track_crawl_result(
                    conn,
                    company["id"],
                    crawl_run_id,
                    crawl_result,
                    observation.completed_at if observation is not None else utc_now(),
                    **tracking_options,
                )
                conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
                savepoint_active = False

            crawl_run_status = crawl_run_status_for_result(
                crawl_result,
                removal_authorization,
            )
            finish_crawl_run(
                conn,
                crawl_run_id,
                summary,
                observation.completed_at if observation is not None else utc_now(),
                status=crawl_run_status,
                error_message=non_success_diagnostic(crawl_run_status, summary),
            )
            conn.commit()
        except Exception as exc:
            if savepoint_active and savepoint_name:
                try:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
                except Exception:
                    pass
            conn.rollback()
            fail_crawl_run(conn, crawl_run_id, str(exc), utc_now())
            conn.commit()
            raise

        # Catalog lifecycle is committed before separately dated, content-only
        # detail recovery. Detail failures cannot undo or renew observations.
        if details and company_slug in {"alignerr", "micro1"} and not crawl_result.used_sample_data and crawl_result.outcome in {ProviderOutcome.SUCCESS, ProviderOutcome.PARTIAL}:
            from wahojobs.crawler.provider_details import update_returned_details
            report = update_returned_details(conn, company_slug, company["id"], crawl_run_id, crawl_result.jobs, mode=details)
            summary = replace(summary, warnings=(*summary.warnings, report))
        return company, summary


def non_success_diagnostic(crawl_run_status, summary):
    if crawl_run_status == "success":
        return None
    details = [
        summary.source_message,
        *summary.removal_skip_reasons,
        *summary.warnings,
    ]
    details = [detail for detail in details if detail]
    return f"{crawl_run_status}: {'; '.join(details)}"


def last_accepted_snapshot_count(conn, company_id):
    row = conn.execute(
        """
        SELECT jobs_found_count
        FROM crawl_runs
        WHERE company_id = ?
          AND status = 'success'
          AND used_sample_data = 0
          AND error_message IS NULL
        ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
        LIMIT 1
        """,
        (company_id,),
    ).fetchone()
    return int(row[0]) if row is not None else None

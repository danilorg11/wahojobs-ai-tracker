"""Explicit local-development target selection for the existing crawler."""
from contextlib import contextmanager, closing
from pathlib import Path
import sqlite3


def local_database_path(value):
    # Reuse the existing offline file/alias validation; no default, creation,
    # migration, database copy, or environment-variable fallback.
    from scripts.google_oidc_authorization_transactions_migration import canonical_database_path
    value = Path(value)
    if not value.is_absolute():
        raise ValueError("Local inventory path must be absolute")
    target = canonical_database_path(value)
    if any((parent / ".git").exists() for parent in target.parents):
        raise ValueError("Local inventory must be outside Git checkouts")
    if any(Path(str(target) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("Local inventory must be quiescent, without SQLite sidecars")
    return target


@contextmanager
def local_inventory_connection(value):
    from wahojobs.database_lifetime_ownership import (
        ROLE_OFFLINE_OPERATOR, acquire_database_lifetime_ownership,
        release_database_lifetime_ownership, require_database_lifetime_ownership,
    )
    target = local_database_path(value)
    ownership = acquire_database_lifetime_ownership(target, role=ROLE_OFFLINE_OPERATOR)
    try:
        require_database_lifetime_ownership(ownership, role=ROLE_OFFLINE_OPERATOR, database_path=target)
        connection = sqlite3.connect(target.as_uri() + "?mode=rw", uri=True)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()
    finally:
        release_database_lifetime_ownership(ownership, role=ROLE_OFFLINE_OPERATOR, database_path=target)


def inspect_refresh(value, sources, *, details=None):
    from wahojobs.crawler.source_registry import assert_production_dispatch_allowed
    from wahojobs.crawler.pipeline import CRAWLERS
    from wahojobs.crawler.provider_details import validate_detail_url
    target = local_database_path(value)
    result = {"database": str(target), "read_only": True, "network_requests": 0,
              "model_calls": False, "details": details, "sources": [],
              "app_configuration": "Use this same path as database_path in the normal durable Google-login configuration. Stop that target's app before refresh; never replace its database file.",
              "detail_policy": "needed: reuse dated, identity-verified accepted details when catalog material has no observed change; fetch missing/changed returned records. all: refetch all returned records. One exact official GET each, no redirects/retries. Future returned URLs/counts are unknown until catalog retrieval; no application-page traversal.",
              "failure_policy": "Failed catalog retrieval does not update jobs. Removal requires the existing complete-snapshot authorization. Detail errors retain accepted content and do not establish availability. Accounts/profiles are not refreshed."}
    with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        for slug in dict.fromkeys(sources):
            assert_production_dispatch_allowed(slug)
            if slug not in CRAWLERS:
                raise ValueError("Unknown source: " + slug)
            company = conn.execute("SELECT id,careers_url FROM companies WHERE slug=?", (slug,)).fetchone()
            if company is None:
                raise ValueError("Source is not configured: " + slug)
            from wahojobs.crawler.providers.mercor import MERCOR_ENDPOINT
            request = {
                "alignerr": {"method": "GET", "pagination": "limit=120, offset=0 then provider-validated pages", "completeness": "Only validated complete snapshots may remove absent records"},
                "mercor": {"method": "GET", "pagination": "One partial listing response", "completeness": "Validated returned records only; never source-wide renewal or absent-record removal"},
                "micro1": {"method": "POST", "pagination": "page=1, limit=100, keyword= then provider-validated pages", "body": {"action": "get_all_jobs", "filters": {"type": ["EXPERT"]}}, "completeness": "Complete validated pagination may remove absent records; partial results cannot"},
            }.get(slug, {"method": "provider-defined", "scope_limitation": "Inspect this existing adapter's pagination and secondary destinations before authorizing; not enumerated by this inspection"})
            if slug == "mercor" and company['careers_url'] != MERCOR_ENDPOINT:
                raise ValueError("Mercor endpoint differs from the supported observation contract")
            item = {"source": slug, "catalog_url": company['careers_url'], **request}
            if slug == "alignerr":
                from wahojobs.crawler.providers.alignerr import add_pagination, MAX_PAGE_SIZE
                item['first_request_url'] = add_pagination(company['careers_url'], MAX_PAGE_SIZE, 0)
            elif slug == "micro1":
                from urllib.parse import urlencode
                from wahojobs.crawler.providers.micro1 import PAGE_LIMIT, REQUEST_BODY
                separator = '&' if '?' in company['careers_url'] else '?'
                item['first_request_url'] = company['careers_url'] + separator + urlencode(dict(page=1, limit=PAGE_LIMIT, keyword=''))
                item['body'] = REQUEST_BODY
            else:
                item['first_request_url'] = company['careers_url']
            item['catalog_transport'] = "Existing adapter pagination/redirect behavior is unchanged; this is an inspection, not a fixed transaction cap."
            if details and slug in {"alignerr", "micro1"}:
                urls=[]
                for row in conn.execute("SELECT external_id,url FROM jobs WHERE company_id=? AND is_active=1 ORDER BY id", (company['id'],)):
                    try: validate_detail_url(slug, row['external_id'], row['url'])
                    except (ValueError, TypeError): continue
                    urls.append(row['url'])
                item.update(known_detail_candidates=urls, detail_host={"alignerr":"www.alignerr.com","micro1":"jobs.micro1.ai"}[slug], detail_path={"alignerr":"/jobs/<returned-id>","micro1":"/post/<returned-id>"}[slug])
            result['sources'].append(item)
    return result

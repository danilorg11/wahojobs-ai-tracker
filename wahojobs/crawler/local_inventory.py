"""Explicit local-development target selection for the existing crawler."""
from contextlib import contextmanager, closing
from pathlib import Path
import sqlite3
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit


MAX_DETAIL_REQUESTS = 500
MAX_HTTP_TRANSACTIONS = 1000
_REQUEST_BUDGET = ContextVar('local_refresh_request_budget', default=None)
_REQUEST_DEADLINE = ContextVar('local_refresh_request_deadline', default=None)
DETAIL_HOSTS = {'www.alignerr.com': 'alignerr', 'jobs.micro1.ai': 'micro1'}


def detail_allocations(sources, limit=None):
    limit = MAX_DETAIL_REQUESTS if limit is None else limit
    selected = sorted(set(sources) & set(DETAIL_HOSTS.values()))
    return {source: limit // len(selected) + int(index < limit % len(selected))
            for index, source in enumerate(selected)}


class RequestBudgetExceeded(OSError):
    pass


@dataclass
class RefreshRequestBudget:
    transactions: list = field(default_factory=list)
    detail_requests: int = 0
    source_details: dict = field(default_factory=dict)
    http_limit: int = MAX_HTTP_TRANSACTIONS
    detail_limit: int = MAX_DETAIL_REQUESTS
    audit_sink: object = None

    def finish_source(self, source):
        if source not in self.source_details or self.source_details[source]['finished']:
            return
        state = self.source_details[source]
        state['finished'] = True
        unused = state['allocation'] - state['requests']
        remaining = sorted(name for name, item in self.source_details.items() if not item['finished'])
        if remaining:
            state['released'] = unused
            for index, name in enumerate(remaining):
                self.source_details[name]['allocation'] += unused // len(remaining) + int(index < unused % len(remaining))

    def reserve(self, request, *, detail=False):
        if len(self.transactions) >= min(self.http_limit, MAX_HTTP_TRANSACTIONS):
            raise RequestBudgetExceeded('Batch HTTP transaction ceiling reached')
        if detail and self.detail_requests >= min(self.detail_limit, MAX_DETAIL_REQUESTS):
            raise RequestBudgetExceeded('Batch detail request ceiling reached')
        source = DETAIL_HOSTS.get(urlsplit(request.full_url).hostname) if detail else None
        state = self.source_details.get(source)
        if detail and self.source_details:
            if state is None or state['finished'] or state['requests'] >= state['allocation']:
                raise RequestBudgetExceeded('Source detail allocation exhausted or unavailable')
        entry = dict(url=request.full_url, method=request.get_method(),
                     kind='detail' if detail else 'catalog',
                     observed_at=datetime.now(timezone.utc).isoformat())
        self.transactions.append(entry)
        self.detail_requests += int(detail)
        if state is not None:
            state['requests'] += 1
        if self.audit_sink is not None:
            self.audit_sink(dict(event='request', ordinal=len(self.transactions), **entry))
        return entry

    def summary(self):
        return dict(http_transactions=len(self.transactions), detail_requests=self.detail_requests,
                    catalog_requests=len(self.transactions)-self.detail_requests,
                    detail_allocations={name: dict(item, unused=item['allocation']-item['requests']-item['released'])
                                        for name, item in self.source_details.items()},
                    http_limit=self.http_limit, detail_limit=self.detail_limit,
                    retries=0, redirected_requests=0, requests=self.transactions)


@contextmanager
def refresh_request_budget(*, sources=(), http_limit=None, detail_limit=None, audit_sink=None):
    http_limit = MAX_HTTP_TRANSACTIONS if http_limit is None else http_limit
    detail_limit = MAX_DETAIL_REQUESTS if detail_limit is None else detail_limit
    if (type(http_limit) is not int or not 0 <= http_limit <= MAX_HTTP_TRANSACTIONS
            or type(detail_limit) is not int or not 0 <= detail_limit <= MAX_DETAIL_REQUESTS
            or audit_sink is not None and not callable(audit_sink)):
        raise ValueError('invalid_refresh_budget')
    allocations = detail_allocations(sources, detail_limit)
    budget = RefreshRequestBudget(source_details={
        name: dict(initial_allocation=amount, allocation=amount, requests=0, released=0,
                   finished=False, recovery=None) for name, amount in allocations.items()},
        http_limit=http_limit, detail_limit=detail_limit, audit_sink=audit_sink)
    token = _REQUEST_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _REQUEST_BUDGET.reset(token)


def reserve_http_request(request, *, detail=False):
    remaining_request_seconds()
    budget = _REQUEST_BUDGET.get()
    return budget.reserve(request, detail=detail) if budget is not None else None


def remaining_request_seconds():
    deadline = _REQUEST_DEADLINE.get()
    remaining = deadline - time.monotonic() if deadline is not None else None
    if remaining is not None and remaining <= 0:
        raise TimeoutError('daily_collection_deadline_expired')
    return remaining


@contextmanager
def request_deadline(monotonic_deadline):
    token = _REQUEST_DEADLINE.set(monotonic_deadline)
    try:
        yield
    finally:
        _REQUEST_DEADLINE.reset(token)


def audit_http_response(entry, *, body=None, error=None, capture_complete=True):
    """Preserve transport evidence before parsing; auditing failure stops acceptance."""
    budget = _REQUEST_BUDGET.get()
    if budget is not None and budget.audit_sink is not None and entry is not None:
        budget.audit_sink(dict(event='response' if body is not None else 'transport_error',
            ordinal=next(i for i, item in enumerate(budget.transactions, 1) if item is entry),
            **entry, **({'raw_response': body, 'capture_complete': capture_complete}
                        if body is not None else {'error_type': error})))


def audit_http_error(entry, exc):
    """Retain bounded HTTP error material without another request or retry."""
    from urllib.error import HTTPError
    if isinstance(exc, HTTPError):
        if entry is not None:
            entry['status'] = exc.code
            entry['final_url'] = exc.geturl()
            entry['response_headers'] = {
                name: exc.headers[name] for name in ('Location', 'Content-Type')
                if exc.headers.get(name) is not None
            }
        try:
            body = exc.read(2_000_001)
            audit_http_response(entry, body=body, capture_complete=len(body) < 2_000_001)
        finally:
            exc.close()
    elif isinstance(getattr(exc, 'partial', None), bytes):
        audit_http_response(entry, body=exc.partial[:2_000_001], capture_complete=False)
    audit_http_response(entry, error=type(exc).__name__)


class _AuditedResponse:
    def __init__(self, response, entry):
        self.response, self.entry = response, entry

    def __getattr__(self, name):
        return getattr(self.response, name)

    def __enter__(self):
        self.response.__enter__()
        return self

    def __exit__(self, *args):
        return self.response.__exit__(*args)

    def read(self, *args):
        try:
            body = self.response.read(*args)
        except Exception as exc:
            audit_http_error(self.entry, exc)
            raise
        audit_http_response(self.entry, body=body)
        return body


def record_detail_recovery(source, counts):
    budget = _REQUEST_BUDGET.get()
    if budget is not None and source in budget.source_details:
        budget.source_details[source]['recovery'] = dict(counts)


def open_catalog(request, *, timeout):
    # Reuse the detail transport's existing no-redirect handler. A 3xx raises
    # before a second request; the final URL is not a substitute for this check.
    from urllib.request import build_opener
    from wahojobs.crawler.provider_details import _NoRedirect
    parsed = urlsplit(request.full_url)
    allowed = {('www.alignerr.com', '/api/jobs'): 'GET',
               ('aws.api.mercor.com', '/work/listings-explore-page'): 'GET',
               ('prod-api.micro1.ai', '/api/v1/job/portal'): 'POST'}
    from wahojobs.daily_source_policy import current_source, validate_request
    validate_request(request)
    if current_source() is None and (parsed.scheme != 'https' or parsed.port is not None or parsed.username
            or parsed.password or parsed.fragment
            or allowed.get((parsed.hostname, parsed.path)) != request.get_method()):
        raise ValueError('Catalog destination/method is outside the supported scope')
    entry = reserve_http_request(request)
    remaining = remaining_request_seconds()
    if remaining is not None:
        timeout = min(timeout, remaining)
    try:
        response = build_opener(_NoRedirect()).open(request, timeout=timeout)
    except OSError as exc:
        if entry is not None:
            entry.update(status=getattr(exc, 'code', None), error=type(exc).__name__)
        audit_http_error(entry, exc)
        raise
    if entry is not None:
        entry['status'] = response.status
        entry['final_url'] = response.geturl()
        entry['response_headers'] = {
            name: response.headers[name] for name in ('Content-Type', 'Content-Length')
            if response.headers.get(name) is not None
        }
        entry['contract_headers'] = {name: response.headers[name] for name in
            ('X-WP-TotalPages', 'X-WP-Total') if response.headers.get(name) is not None}
    return _AuditedResponse(response, entry) if entry is not None else response


def open_public(request, *, timeout):
    """Legacy public adapters share the audited transport inside a daily plan."""
    from wahojobs.daily_source_policy import current_source
    if current_source() is not None:
        if _REQUEST_BUDGET.get() is None:raise ValueError('daily_request_budget_required')
        return open_catalog(request, timeout=timeout)
    from urllib.request import urlopen
    return urlopen(request, timeout=timeout)


def record_surface_counts(*, upstream_records, upstream_unit, variants, filtered=0):
    budget = _REQUEST_BUDGET.get()
    if budget is not None and budget.audit_sink is not None:
        budget.audit_sink(dict(event='surface_counts', upstream_records=upstream_records,
            upstream_unit=upstream_unit, variants=variants, filtered_records=filtered))


def record_envelope_shape(payload):
    budget = _REQUEST_BUDGET.get()
    if budget is not None and budget.audit_sink is not None and isinstance(payload, dict):
        # Raw envelope is already retained by transport. Report only shape and
        # continuation presence; never guess or dispatch an undocumented cursor.
        names=('nextPage','nextCursor','cursor','hasMore','pagination','total','totalCount','offset','limit')
        budget.audit_sink(dict(event='envelope_shape',keys=sorted(payload),
            pagination_signals={k:bool(payload[k]) for k in names if k in payload}))


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
def local_inventory_connection(value, *, ownership=None):
    from wahojobs.database_lifetime_ownership import (
        ROLE_OFFLINE_OPERATOR, acquire_database_lifetime_ownership,
        release_database_lifetime_ownership, require_database_lifetime_ownership,
    )
    target = local_database_path(value)
    acquired = ownership is None
    if acquired:
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
        if acquired:
            release_database_lifetime_ownership(ownership, role=ROLE_OFFLINE_OPERATOR, database_path=target)


def inspect_refresh(value, sources, *, details=None):
    from wahojobs.crawler.source_registry import assert_production_dispatch_allowed
    from wahojobs.crawler.pipeline import CRAWLERS
    from wahojobs.crawler.provider_details import validate_detail_url
    target = local_database_path(value)
    result = {"database": str(target), "read_only": True, "network_requests": 0,
              "model_calls": False, "details": details, "sources": [],
              "limits": {"http_transactions": MAX_HTTP_TRANSACTIONS, "detail_requests": MAX_DETAIL_REQUESTS,
                         "detail_timeout_seconds": 25, "detail_response_bytes": 2000000,
                         "retries": 0, "catalog_redirects": "rejected before dispatch", "detail_redirects": "rejected before dispatch"},
              "app_configuration": "Use this same path as database_path in the normal durable Google-login configuration. Stop that target's app before refresh; never replace its database file.",
              "detail_policy": "needed: reuse dated, identity-verified accepted details when catalog material has no observed change; fetch missing/changed returned records. all: refetch all returned records. One exact official GET each, no redirects/retries. Future returned URLs/counts are unknown until catalog retrieval; no application-page traversal.",
              "failure_policy": "Failed catalog retrieval does not update jobs. Removal requires the existing complete-snapshot authorization. Detail errors retain accepted content and do not establish availability. Accounts/profiles are not refreshed."}
    result['detail_allocations'] = detail_allocations(sources) if details else {}
    result['detail_allocation_policy'] = "Equal reserved HTTP allowances by selected detail source; remainder by source name. Unused allowance passes to sources still awaiting their turn. No second pass. Compatible reuse costs no allowance; missing usable details precede rechecks. Global ceilings still apply; pending counts are reported after ingestion, not inferred from stored catalog size."
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
                from wahojobs.crawler.providers.alignerr import add_pagination, MAX_PAGE_SIZE, MAX_PAGES, MAX_RECORDS, REQUEST_TIMEOUT_SECONDS
                item['first_request_url'] = add_pagination(company['careers_url'], MAX_PAGE_SIZE, 0)
                item['limits'] = dict(pages=MAX_PAGES, records=MAX_RECORDS, timeout_seconds=REQUEST_TIMEOUT_SECONDS)
            elif slug == "micro1":
                from urllib.parse import urlencode
                from wahojobs.crawler.providers.micro1 import PAGE_LIMIT, REQUEST_BODY, MAX_PAGES, MAX_RECORDS
                separator = '&' if '?' in company['careers_url'] else '?'
                item['first_request_url'] = company['careers_url'] + separator + urlencode(dict(page=1, limit=PAGE_LIMIT, keyword=''))
                item['body'] = REQUEST_BODY
                item['limits'] = dict(pages=MAX_PAGES, records=MAX_RECORDS, timeout_seconds=60)
            else:
                item['first_request_url'] = company['careers_url']
                if slug == 'mercor': item['limits'] = dict(pages=1, timeout_seconds=30)
            from wahojobs.daily_source_policy import POLICY
            if slug in POLICY:
                item['daily_contract']=POLICY[slug]
            item['catalog_transport'] = "Daily ready-source execution: exact endpoint/query/body checks, shared audited budget, no retries, redirects rejected before dispatch. Legacy standalone adapters retain their existing transport policy."
            if details and slug in {"alignerr", "micro1"}:
                urls=[]
                for row in conn.execute("SELECT external_id,url FROM jobs WHERE company_id=? AND is_active=1 ORDER BY id", (company['id'],)):
                    try: validate_detail_url(slug, row['external_id'], row['url'])
                    except (ValueError, TypeError): continue
                    urls.append(row['url'])
                item.update(known_detail_candidates=urls, detail_host={"alignerr":"www.alignerr.com","micro1":"jobs.micro1.ai"}[slug], detail_path={"alignerr":"/jobs/<returned-id>","micro1":"/post/<returned-id>"}[slug])
            result['sources'].append(item)
    return result

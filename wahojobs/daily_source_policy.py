"""Closed daily scopes for the existing core adapters, not a crawler registry.

Readiness is code-reviewed. Owner configuration may reduce a ready budget or
disable a source; it cannot admit a blocked contract or widen endpoint scope.
The September 4 execution receipts establish access, not provider-wide coverage.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import re
from urllib.parse import parse_qs, urlsplit

CORE_SOURCES = ('alignerr', 'appen', 'dataannotation', 'dataforce', 'handshake',
    'meridial', 'mercor', 'micro1', 'mindrift', 'oneforma', 'outlier', 'rws',
    'surge', 'turing', 'welocalize')
ALERT_RECIPIENT = 'danilo@wahojobs.com'
_DAILY_SOURCE = ContextVar('daily_inventory_source', default=None)
_CONTROLLED_VALIDATION = ContextVar('controlled_source_validation', default=False)
_DATAANNOTATION_OBSERVED_REDIRECT = ContextVar('dataannotation_observed_redirect', default=None)
_DATAANNOTATION_DOMAINS = ContextVar('dataannotation_controlled_domains', default=None)
_DATAFORCE_OBSERVED_DETAILS = ContextVar('dataforce_observed_details', default=frozenset())
_SURGE_OBSERVED_DETAILS = ContextVar('surge_observed_details', default=frozenset())
_HANDSHAKE_OBSERVED_ASSETS = ContextVar('handshake_observed_assets', default=frozenset())


def entry(requests, seconds, expected, scope, rule, *, blocker=None, correction=None, cooldown=0):
    return dict(http_max=requests, seconds_max=seconds, retained_http=expected,
        endpoint_scope=scope, verification_rule=rule, cooldown_hours=cooldown,
        readiness='blocked' if blocker else 'ready', blocker=blocker,
        corrective_action=correction, live_validation_required=True,
        admission_condition=('Repair and fixture-test the stated contract, then bounded public validation.'
            if blocker else 'Verify current allowlisted surface and native activation checks in one controlled run.'))


POLICY = {
    'alignerr': entry(100, 360, 47,
        ['GET https://www.alignerr.com/api/jobs?limit=120&offset=<validated offset>'],
        'Stable total, offset/limit, unique IDs, exact final count; no closure on cap/interruption.'),
    'appen': entry(1, 60, 1,
        ['GET https://api.lever.co/v0/postings/appen?mode=json&expand=location'],
        'Complete public board list with required fields and unique IDs; no authenticated or corporate search.'),
    'dataannotation': entry(20, 360, 20,
        ['GET https://www.dataannotation.tech/'+p for p in
         ('coding','generalist','law','math','medicine','physics','finance','accounting','chemistry','biology')],
        'Ten exact evergreen role routes and individually attested canonical pages; no absence closure or active-project claim.'),
    'dataforce': entry(15, 360, 11,
        ['GET https://dataforcecommunity.transperfect.com/projects',
         'GET https://dataforcecommunity.transperfect.com/projects?project_type=All&page=<1..19>',
         'GET up to remaining cap of exact index-linked https://dataforcecommunity.transperfect.com/(project|study)/<slug>'],
        'Eight exact individually attested remote Thyme roles; remaining daily request slots rotate other remote index cards for inspection only. Partial source, no absence closure.'),
    'handshake': entry(40, 120, 29,
        ['GET https://joinhandshake.com/ai/opportunities[/]',
         'GET page-linked https://framerusercontent.com/sites/<public module>.mjs',
         'GET module-linked https://framerusercontent.com/cms/<public collection>.framercms'],
        'Individually attested remote AI public CMS records only; bounded linked modules and complete declared chunk coverage per observation. Partial individual authority, no absence closure or active-project claim.'),
    'meridial': entry(2, 150, 2,
        ['GET https://boards-api.greenhouse.io/v1/boards/agency/jobs?content=true',
         'GET https://boards-api.greenhouse.io/v1/boards/agency/departments/4012485101?render_as=tree'],
        'Existing approved Greenhouse jobs/meta.total and AI department tree crosscheck, exact record attestations and count-drop guard.'),
    'mercor': entry(1, 60, 1,
        ['GET https://aws.api.mercor.com/work/listings-explore-page'],
        'Partial public listings array only: unique exact active, nonprivate, nondeleted records; absence never closes. No retained pagination contract proves provider-wide completeness.'),
    'micro1': entry(50, 240, 4,
        ['POST https://prod-api.micro1.ai/api/v1/job/portal?page=<1..50>&limit=100&keyword= ; body {"action":"get_all_jobs","filters":{"type":["EXPERT"]}}'],
        'Stable declared total and exact unique-ID pagination; native 50-page ceiling; optional /post detail requests excluded.'),
    'mindrift': entry(70, 360, 17,
        ['GET https://apply.workable.com/toloka-ai/llms.txt',
         'GET https://apply.workable.com/toloka-ai/jobs.md',
         'POST https://apply.workable.com/api/v3/accounts/toloka-ai/jobs ; body {} then {"token":"<returned nextPage>"}'],
        'Stable total, unique shortcodes, terminal nextPage; publish only public published records. Two probes plus at most 68 token pages, 0.2s spacing, zero retries.', cooldown=12),
    'oneforma': entry(3, 210, 1,
        ['GET https://www.oneforma.com/wp-json/wp/v2/job?per_page=100&_embed=wp:term&page=<1..3>'],
        'Stable X-WP-TotalPages and unique post IDs; each post may emit many language/application variants; cap before final page is incomplete.'),
    'outlier': entry(1, 60, 1,
        ['POST https://app.outlier.ai/internal/experts/job-board/jobs ; body {}'],
        'Existing unauthenticated public job board; no inference from endpoint path name.',
        blocker='Legacy wrapper falls back to synthetic jobs on empty/error and lacks validated public record/snapshot authority.',
        correction='Remove daily sample fallback before tracking; retain real envelope and validate visibility, IDs and supported promotion contract.'),
    'rws': entry(1, 60, 1,
        ['GET https://api.lever.co/v0/postings/rws?mode=json&expand=location'],
        'Complete validated Lever list with existing TrainAI keyword filter and category transformation; excluded corporate postings counted separately.'),
    'surge': entry(20, 360, 9,
        ['GET https://surgehq.ai/workforce',
         'GET index-linked https://surgehq.ai/workforce/<slug> (at most 18)',
         'GET https://surgehq.ai/fellowship'],
        'Seven index-linked remote AI workforce roles with canonical detail and role-bound application evidence; individual public-inventory record authority only, no absence closure. Fellowship remains excluded until separately attested.'),
    'turing': entry(3, 210, 1,
        ['POST https://work.turing.com/api/jobs/all ; body {"searchQuery":"","expertise":[],"location":[],"pageNumber":<1..3>,"pageSize":500,"sortingCriteria":"newest"}'],
        'success plus stable totalCount, unique IDs and exact pagination; at most 1500 returned rows under this policy, not provider-wide completeness.'),
    'welocalize': entry(1, 90, 1,
        ['GET https://api.lever.co/v0/postings/weloglobal?mode=json&expand=location'],
        'Complete validated Lever list, exact Welo Data - AI Services filter; excluded board records are neither rejected nor variants.'),
}
READY_SOURCES = tuple(s for s in CORE_SOURCES if POLICY[s]['readiness'] == 'ready')
NEW_SCOPES_REQUIRE_EXPLICIT_CONFIGURATION = frozenset({'dataannotation', 'dataforce', 'handshake', 'surge'})
OVERHEAD_SECONDS = 240  # stop/preflight queries, one backup, final integrity, process cleanup
MAX_EXECUTION_SECONDS = 2520  # installed daily service and approved collection ceiling


def default_sources():
    return {s: dict(enabled=s in READY_SOURCES and s not in NEW_SCOPES_REQUIRE_EXPLICIT_CONFIGURATION,
        http_max=POLICY[s]['http_max'],
        seconds_max=POLICY[s]['seconds_max']) for s in CORE_SOURCES}


def validate_sources(configured):
    if type(configured) is not dict or set(configured) != set(CORE_SOURCES):
        raise ValueError('all_core_sources_must_be_accounted_for')
    for source, row in configured.items():
        if type(row) is not dict or set(row) != {'enabled','http_max','seconds_max'} or type(row['enabled']) is not bool:
            raise ValueError('invalid_source_policy')
        if row['enabled'] and source not in READY_SOURCES:
            raise ValueError('blocked_source_cannot_be_enabled:'+source)
        for field in ('http_max','seconds_max'):
            if type(row[field]) is not int or not 0 < row[field] <= POLICY[source][field]:
                raise ValueError('incompatible_source_budget:'+source)
    if not any(r['enabled'] for r in configured.values()):raise ValueError('no_daily_sources_enabled')
    if aggregate(configured)['execution_seconds'] > MAX_EXECUTION_SECONDS:
        raise ValueError('daily_execution_ceiling_exceeded')
    return configured


def aggregate(configured):
    active = [r for r in configured.values() if r['enabled']]
    return dict(http_max=sum(r['http_max'] for r in active),
        execution_seconds=OVERHEAD_SECONDS+sum(r['seconds_max'] for r in active))


def current_source():return _DAILY_SOURCE.get()


@contextmanager
def observed_handshake_assets(urls):
    """Admit only exact assets extracted from the current public page/module."""
    if type(urls) not in (tuple, list) or len(urls) > 64 or len(urls) != len(set(urls)):
        raise ValueError('handshake_asset_scope_invalid')
    for url in urls:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.netloc != 'framerusercontent.com'
                or parsed.query or parsed.fragment or parsed.port
                or not (re.fullmatch(r'/sites/[A-Za-z0-9_-]+/[A-Za-z0-9_.@-]+\.mjs', parsed.path)
                        or re.fullmatch(r'/cms/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+-chunk-default-\d+\.framercms', parsed.path))):
            raise ValueError('handshake_asset_scope_invalid')
    token = _HANDSHAKE_OBSERVED_ASSETS.set(frozenset(urls))
    try: yield
    finally: _HANDSHAKE_OBSERVED_ASSETS.reset(token)
def controlled_validation_active():return _CONTROLLED_VALIDATION.get()
def controlled_dataannotation_domains():
    return (_DATAANNOTATION_DOMAINS.get() if current_source() == 'dataannotation'
            and controlled_validation_active() else None)


@contextmanager
def controlled_dataannotation_domain_subset(domains):
    """Narrow one controlled run to fixed pages already in the source plan."""
    allowed = {'coding','generalist','law','math','medicine','physics','finance',
        'accounting','bilingual','chemistry','biology'}
    if not domains or len(domains) != len(set(domains)) or set(domains) - allowed:
        raise ValueError('dataannotation_controlled_subset_invalid')
    token = _DATAANNOTATION_DOMAINS.set(frozenset(domains))
    try:yield
    finally:_DATAANNOTATION_DOMAINS.reset(token)


@contextmanager
def observed_dataannotation_redirect(requested_url, destination):
    """Admit one exact same-site Location observed from a fixed source page."""
    start, target = urlsplit(requested_url), urlsplit(destination)
    fixed = {'/'+name for name in ('coding','generalist','law','math','medicine',
        'physics','finance','accounting','bilingual','chemistry','biology')}
    known = {'/coding': '/job-board/software-engineer',
             '/generalist': '/job-board/generalist',
             '/law': '/job-board/legal-expert',
             '/math': '/job-board/mathematician',
             '/medicine': '/job-board/medical-expert',
             '/physics': '/job-board/physicist',
             '/finance': '/job-board/finance-expert',
             '/accounting': '/job-board/accountant',
             '/chemistry': '/job-board/chemist',
             '/biology': '/job-board/biologist'}
    if (start.scheme != 'https' or start.netloc != 'www.dataannotation.tech'
            or start.path not in fixed or start.query or start.fragment
            or target.scheme != 'https' or target.netloc != start.netloc
            or target.query or target.fragment
            or target.path != known.get(start.path)):
        raise ValueError('dataannotation_observed_redirect_out_of_scope')
    token = _DATAANNOTATION_OBSERVED_REDIRECT.set(destination)
    try: yield
    finally: _DATAANNOTATION_OBSERVED_REDIRECT.reset(token)


@contextmanager
def observed_dataforce_details(urls):
    """Admit only exact project/study URLs already parsed from this index."""
    if type(urls) not in (tuple, list) or len(urls) > 50 or len(urls) != len(set(urls)):
        raise ValueError('dataforce_detail_scope_invalid')
    for url in urls:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.netloc != 'dataforcecommunity.transperfect.com'
                or parsed.query or parsed.fragment
                or re.fullmatch(r'/(?:project|study)/[A-Za-z0-9-]+', parsed.path) is None):
            raise ValueError('dataforce_detail_scope_invalid')
    token = _DATAFORCE_OBSERVED_DETAILS.set(frozenset(urls))
    try: yield
    finally: _DATAFORCE_OBSERVED_DETAILS.reset(token)


@contextmanager
def observed_surge_details(urls):
    if type(urls) not in (tuple, list) or not 0 < len(urls) <= 18 or len(urls) != len(set(urls)):
        raise ValueError('surge_detail_scope_invalid')
    for url in urls:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.netloc != 'surgehq.ai'
                or parsed.query or parsed.fragment
                or re.fullmatch(r'/workforce/[A-Za-z0-9-]+', parsed.path) is None):
            raise ValueError('surge_detail_scope_invalid')
    token = _SURGE_OBSERVED_DETAILS.set(frozenset(urls))
    try: yield
    finally: _SURGE_OBSERVED_DETAILS.reset(token)


@contextmanager
def daily_source(source):
    if source not in READY_SOURCES:raise ValueError('source_not_ready_for_daily:'+source)
    token = _DAILY_SOURCE.set(source)
    try:yield
    finally:_DAILY_SOURCE.reset(token)


@contextmanager
def controlled_validation_source(source):
    # An isolated read-only observation, never daily activation.
    if source not in ('dataannotation', 'dataforce'):
        raise ValueError('controlled_validation_source_out_of_scope')
    token = _DAILY_SOURCE.set(source)
    validation_token = _CONTROLLED_VALIDATION.set(True)
    try: yield
    finally:
        _CONTROLLED_VALIDATION.reset(validation_token)
        _DAILY_SOURCE.reset(token)


def validate_request(request):
    """No redirect, credentials, alternate query/body, detail or cross-source dispatch."""
    source = current_source()
    if source is None:return
    p = urlsplit(request.full_url)
    if p.scheme != 'https' or p.port is not None or p.username or p.password or p.fragment:
        raise ValueError('daily_endpoint_out_of_scope')
    if any(k.lower() in ('authorization','cookie','proxy-authorization') for k, _ in request.header_items()):
        raise ValueError('daily_private_headers_forbidden')
    query = parse_qs(p.query, keep_blank_values=True, strict_parsing=True)
    method = request.get_method(); body = json.loads(request.data) if request.data else None
    def at(host, path, verb='GET'):return p.hostname == host and p.path == path and method == verb
    def integer(name, minimum, maximum):
        v = query.get(name, [])
        return len(v)==1 and v[0].isdecimal() and minimum <= int(v[0]) <= maximum
    ok = False
    if source == 'alignerr':
        ok = at('www.alignerr.com','/api/jobs') and set(query)=={'limit','offset'} and integer('limit',1,120) and integer('offset',0,19999)
    elif source in ('appen','rws','welocalize'):
        board = 'weloglobal' if source == 'welocalize' else source
        ok = at('api.lever.co','/v0/postings/'+board) and query == {'mode':['json'],'expand':['location']}
    elif source == 'dataannotation':
        paths = {'/'+name for name in (
            'coding','generalist','law','math','medicine','physics',
            'finance','accounting','chemistry','biology')}
        if controlled_validation_active():
            paths.add('/bilingual')
        paths.add('/job-board/software-engineer')
        observed = _DATAANNOTATION_OBSERVED_REDIRECT.get()
        if observed is not None:
            paths.add(urlsplit(observed).path)
        ok = p.netloc == 'www.dataannotation.tech' and p.path in paths and method == 'GET' and not query
    elif source == 'dataforce':
        ok = p.netloc == 'dataforcecommunity.transperfect.com' and method == 'GET' and (
            p.path == '/projects' and not query or
            p.path == '/projects' and set(query) == {'project_type','page'}
            and query['project_type'] == ['All'] and integer('page',1,19) or
            request.full_url in _DATAFORCE_OBSERVED_DETAILS.get())
    elif source == 'surge':
        ok = (p.netloc == 'surgehq.ai' and method == 'GET' and not query
              and (p.path in ('/workforce','/fellowship')
                   or request.full_url in _SURGE_OBSERVED_DETAILS.get()))
    elif source == 'handshake':
        ok = (method == 'GET' and not query and (
            p.netloc == 'joinhandshake.com' and p.path == '/ai/opportunities/'
            or request.full_url in _HANDSHAKE_OBSERVED_ASSETS.get()))
    elif source == 'meridial':
        ok = (at('boards-api.greenhouse.io','/v1/boards/agency/jobs') and query=={'content':['true']} or
              at('boards-api.greenhouse.io','/v1/boards/agency/departments/4012485101') and query=={'render_as':['tree']})
    elif source == 'mercor':ok = at('aws.api.mercor.com','/work/listings-explore-page') and not query
    elif source == 'micro1':
        ok = at('prod-api.micro1.ai','/api/v1/job/portal','POST') and set(query)=={'page','limit','keyword'} and integer('page',1,50) and query['limit']==['100'] and query['keyword']==[''] and body=={'action':'get_all_jobs','filters':{'type':['EXPERT']}}
    elif source == 'mindrift':
        ok = (at('apply.workable.com','/toloka-ai/llms.txt') or at('apply.workable.com','/toloka-ai/jobs.md')) and not query
        if method == 'POST':
            ok = at('apply.workable.com','/api/v3/accounts/toloka-ai/jobs','POST') and not query and (body=={} or type(body) is dict and set(body)=={'token'} and type(body['token']) is str and 0<len(body['token'])<=4096)
    elif source == 'oneforma':
        ok = at('www.oneforma.com','/wp-json/wp/v2/job') and set(query)=={'per_page','_embed','page'} and query['per_page']==['100'] and query['_embed']==['wp:term'] and integer('page',1,3)
    elif source == 'turing':
        ok = at('work.turing.com','/api/jobs/all','POST') and not query and type(body) is dict and type(body.get('pageNumber')) is int and 1<=body['pageNumber']<=3 and body==dict(searchQuery='',expertise=[],location=[],pageNumber=body['pageNumber'],pageSize=500,sortingCriteria='newest')
    if not ok or method=='GET' and body is not None:raise ValueError('daily_endpoint_out_of_scope:'+source)

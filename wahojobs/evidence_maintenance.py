"""Explicit existing-source maintenance. No scheduler or implicit model work.

Plans and receipts are local operator artifacts, not database source authority.
Production acceptance, lifecycle, preparation and storage contracts remain owners
of every evidence mutation. Journal files are append-only, exclusive and fsynced.
"""
from contextlib import contextmanager, closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

from wahojobs.crawler.local_inventory import (
    inspect_refresh, local_database_path, local_inventory_connection,
    refresh_request_budget, MAX_HTTP_TRANSACTIONS, MAX_DETAIL_REQUESTS,
)
from wahojobs.crawler.pipeline import run_crawl
from wahojobs.db.repository import get_job_source_capture_evidence
from wahojobs.opportunity_enrichment import (
    load_semantic_input, classify_enrichment_freshness, enrich_selected_opportunities,
)

VERSION = 'evidence_maintenance_v1'
PROVIDERS = ('alignerr', 'mercor')
ROOT = Path(__file__).resolve().parents[1]


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return sha256(encoded(value)).hexdigest()


def clock_now():
    return datetime.now(timezone.utc)


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('maintenance_aware_clock_required')
    return value.astimezone(timezone.utc)


@contextmanager
def read_connection(path):
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA query_only=ON')
        yield connection


def contract_fingerprint():
    # Bind the actual installed implementation, including acceptance/recipes.
    paths = sorted([*ROOT.joinpath('wahojobs').rglob('*.py'),
                    *ROOT.joinpath('wahojobs/db').rglob('*.sql'),
                    ROOT/'wahojobs/crawler/source_registry.json',
                    ROOT/'scripts/profile_match_digest.py'])
    return digest({p.relative_to(ROOT).as_posix(): sha256(p.read_bytes()).hexdigest() for p in paths})


def database_identity(path):
    stat = path.stat()
    return dict(path=str(path), device=stat.st_dev, inode=stat.st_ino)


def journal_binding(target):
    pin = target.with_name(target.name + '.evidence-maintenance.json')
    if not pin.exists():
        return None
    binding = json.loads(pin.read_text(encoding='utf-8'))
    if binding.get('database') != database_identity(target):
        raise ValueError('maintenance_journal_database_identity_changed')
    return binding


def pin_journal(target, root):
    """Database-local durable recovery location; never silently replace it."""
    expected = dict(database=database_identity(target), journal_root=str(Path(root).resolve()))
    binding = journal_binding(target)
    if binding is not None and binding != expected:
        raise ValueError('maintenance_journal_root_changed')
    if binding is None:
        save_json(target.with_name(target.name + '.evidence-maintenance.json'), expected)


def schema_fingerprint(connection):
    return digest([tuple(r) for r in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")])


def source_fingerprint(connection, slug):
    """Only selected public inventory/derived rows, never private profiles."""
    company = connection.execute('SELECT * FROM companies WHERE slug=?', (slug,)).fetchone()
    if company is None:
        return digest(dict(missing=slug))
    cid = company['id']
    material = dict(company=dict(company))
    for table, column in (('jobs', 'company_id'), ('crawl_runs', 'company_id'),
                          ('canonical_opportunities', 'company_id')):
        material[table] = [dict(r) for r in connection.execute(
            'SELECT * FROM ' + table + ' WHERE ' + column + '=? ORDER BY id', (cid,))]
    for table in ('job_source_contents', 'job_source_content_acceptances', 'job_source_content_captures'):
        material[table] = [dict(r) for r in connection.execute(
            'SELECT s.* FROM ' + table + ' s JOIN jobs j ON j.id=s.job_id '
            'WHERE j.company_id=? ORDER BY s.rowid', (cid,))]
    for table in ('opportunity_enrichments', 'opportunity_enrichment_overrides'):
        material[table] = [dict(r) for r in connection.execute(
            'SELECT e.* FROM ' + table + ' e JOIN canonical_opportunities co '
            'ON co.id=e.canonical_opportunity_id WHERE co.company_id=? ORDER BY e.rowid', (cid,))]
    return digest(material)


def inspect_source(connection, slug, now):
    from scripts.profile_match_digest import get_active_rows
    from wahojobs.matching.opportunity_trust import assess_opportunity_trust
    from wahojobs.crawler.provider_details import DETAIL_KEY
    company = connection.execute('SELECT * FROM companies WHERE slug=?', (slug,)).fetchone()
    if company is None:
        return dict(provider=slug, input_status='source_not_configured', jobs=[], enrichments=[],
                    fingerprint=source_fingerprint(connection, slug))
    active = {r['job_id']: dict(r) for r in get_active_rows(connection, source_slugs=[slug])}
    jobs, enrichments = [], []
    rows = connection.execute('SELECT * FROM jobs WHERE company_id=? ORDER BY id', (company['id'],)).fetchall()
    for row in rows:
        evidence = get_job_source_capture_evidence(connection, row['id'])
        source = connection.execute('SELECT * FROM job_source_contents WHERE job_id=?', (row['id'],)).fetchone()
        detail = json.loads(source['metadata_json'] or '{}').get(DETAIL_KEY, {}) if source else {}
        if not source or not source['body']:
            description = 'missing_accepted_body'
        elif slug == 'alignerr' and not detail.get('display_text'):
            description = 'catalog_only_not_full_detail'
        elif slug == 'alignerr':
            description = 'accepted_full_detail'
        else:
            description = 'accepted_listing_body'
        trust = (assess_opportunity_trust(active[row['id']], 'unknown', now=now).as_dict()
                 if row['id'] in active else dict(status='inactive' if not row['is_active'] else 'unavailable'))
        jobs.append(dict(job_id=row['id'], canonical_id=row['canonical_opportunity_id'],
            external_id=row['external_id'], url=row['url'], verification=trust,
            description=description, evidence=evidence))
    for canonical in sorted({r['canonical_opportunity_id'] for r in rows if r['canonical_opportunity_id']}):
        semantic = load_semantic_input(connection, canonical)
        stored = connection.execute('SELECT * FROM opportunity_enrichments WHERE canonical_opportunity_id=?',
                                    (canonical,)).fetchone()
        enrichments.append(dict(canonical_id=canonical, **classify_enrichment_freshness(semantic, stored)))
    latest = connection.execute('SELECT * FROM crawl_runs WHERE company_id=? ORDER BY id DESC LIMIT 1',
                                (company['id'],)).fetchone()
    return dict(provider=slug, input_status='available', jobs=jobs, enrichments=enrichments,
                latest_run=dict(latest) if latest else None, fingerprint=source_fingerprint(connection, slug))


class OwnerPreparation:
    """The existing authenticated service/preparer, narrowed to one selection.

    Credential input stays in memory. CLI constructs the same production read
    authorization service; embedding callers may use their runtime's service.
    """
    def __init__(self, service, provider, preparer, *, owner, profile_id, job_ids, credentials):
        if not owner or not profile_id:
            raise ValueError('maintenance_owner_scope_required')
        self.service, self.provider, self.preparer = service, provider, preparer
        self.owner, self.profile_id, self.job_ids = tuple(owner), profile_id, list(job_ids)
        self.credentials = credentials

    def inspect(self):
        result = self.service.resolve(method='POST', **self.credentials)
        if result.state != 'profile':
            raise ValueError('maintenance_owner_authorization_denied')
        context = result.authorized_state().professional_background_context(self.preparer.evidence)
        if context is None or tuple(context.owner) != self.owner or context.profile_id != self.profile_id:
            raise ValueError('maintenance_owner_scope_mismatch')
        plan = self.preparer.prepare(self.service, self.provider, profile_id=self.profile_id,
                                    job_ids=self.job_ids, **self.credentials)
        # Reports expose references and reservations, not raw private model input.
        return dict(owner=list(self.owner), profile_id=self.profile_id, job_ids=self.job_ids,
            recipe=self.preparer.evidence.recipe, model=self.preparer.evidence.model,
            basis=self.preparer.evidence.basis,
            store_path=str(self.preparer.evidence._store.path)
                if self.preparer.evidence._store is not None else None,
            execution_configuration=dict(allow_real=self.preparer._allow_real,
                client_type=type(self.preparer._client).__name__, service_tier=getattr(self.preparer._client, 'service_tier', None)),
            **{k: v for k, v in plan.items() if k not in ('items', 'profile_id')},
            items=[{k: v for k, v in item.items() if k not in ('model_input', 'clause')} for item in plan['items']])

    def execute(self, plan_id, journal):
        # Existing attempt/CAS/store accounting owns consumption. Chain the sink
        # so raw model responses are durable before publication.
        prior = self.preparer._audit_sink
        def audit(event):
            journal.append('preparation_audit', event)
            if prior is not None:
                prior(event)
        self.preparer._audit_sink = audit
        try:
            return self.preparer.prepare(self.service, self.provider, profile_id=self.profile_id,
                job_ids=self.job_ids, **self.credentials, execute=True, authorized=True,
                expected_plan_id=plan_id)
        finally:
            self.preparer._audit_sink = prior


class EnrichmentRepair:
    """One selected canonical through the existing enrichment acceptance API.

    Uses the existing reservation value object and bounded structured transport.
    The claim is durable before dispatch; neither a restart nor another plan can
    retry an uncertain binding in this operator journal.
    """
    def __init__(self, canonical_id, client, budget, journal_root):
        from wahojobs.professional_background_preparation import PreparationBudget
        from wahojobs.opportunity_llm import OpenAIStructuredEnrichmentClient
        if (type(canonical_id) is not int or canonical_id <= 0
                or type(budget) is not PreparationBudget
                or not isinstance(client, OpenAIStructuredEnrichmentClient)):
            raise ValueError('maintenance_enrichment_configuration_required')
        self.canonical_id, self.client, self.budget = canonical_id, client, budget
        self.journal_root = Path(journal_root).resolve()

    def inspect(self, connection):
        from decimal import Decimal
        from wahojobs import opportunity_enrichment as oe
        from wahojobs.opportunity_llm import (MAX_OUTPUT_TOKENS, system_prompt, structured_output_schema)
        from wahojobs.source_clause_materiality import binding_fingerprint
        row = connection.execute('SELECT c.slug FROM canonical_opportunities co JOIN companies c '
            'ON c.id=co.company_id WHERE co.id=?', (self.canonical_id,)).fetchone()
        if row is None:
            raise ValueError('maintenance_enrichment_source_unavailable')
        semantic = load_semantic_input(connection, self.canonical_id)
        stored = connection.execute('SELECT * FROM opportunity_enrichments WHERE canonical_opportunity_id=?',
                                    (self.canonical_id,)).fetchone()
        freshness = classify_enrichment_freshness(semantic, stored)
        packet, blocks = oe.llm_source_packet(semantic)
        schema = structured_output_schema(sorted(blocks), clause_ids=[c['clause_id'] for c in packet.get('qualification_clauses', [])])
        input_bytes = len(encoded(dict(prompt=system_prompt(), packet=packet, schema=schema)))
        tokens, usd = self.budget.reservation(input_bytes, output_tokens=MAX_OUTPUT_TOKENS)
        binding = binding_fingerprint(semantic)
        identity = dict(canonical_id=self.canonical_id, input_sha256=oe.semantic_input_sha256(semantic),
            binding=binding, provider=self.client.provider, model=self.client.model, prompt=self.client.prompt_version)
        key = digest(identity)
        target = Path(connection.execute('PRAGMA database_list').fetchone()[2]).resolve()
        pin = journal_binding(target)
        attempted = oe.has_llm_attempt(connection, self.canonical_id, identity['input_sha256'],
            self.client, clause_binding_sha256=binding if freshness['clause_binding_status'] in ('stale','invalid') else None)
        needed = not (stored and freshness['freshness'] == 'current' and stored['model_provider'] == self.client.provider
                      and stored['model_name'] == self.client.model and stored['prompt_version'] == self.client.prompt_version)
        blocks = []
        if pin is not None and pin['journal_root'] != str(self.journal_root):
            blocks.append('maintenance_journal_root_changed')
        offline = getattr(self.client, 'offline_labelled_stub', False) is True
        if not oe.has_sufficient_llm_source_content(semantic):
            blocks.append('enrichment_source_input_unavailable')
        if attempted or (self.journal_root/'enrichment-attempts'/f'{key}.json').exists():
            blocks.append('enrichment_attempt_already_consumed')
        if tokens > self.budget.token_limit or (not offline and (usd > Decimal(self.budget.usd_limit)
                or any(Decimal(v) <= 0 for v in (self.budget.usd_limit, self.budget.input_usd_per_million,
                                                 self.budget.output_usd_per_million)))):
            blocks.append('enrichment_budget_unavailable')
        return dict(provider=row[0], canonical_id=self.canonical_id, needed=needed, blocked=blocks,
            identity=identity, attempt_key=key, journal_root=str(self.journal_root), freshness=freshness,
            model=self.client.model, prompt_version=self.client.prompt_version,
            service_tier=self.client.service_tier, client_type=type(self.client).__name__, offline_labelled_stub=offline,
            budget=asdict(self.budget), reservation=dict(tokens=tokens, usd=str(usd)),
            input_bytes=input_bytes, maximum_physical_requests=1, maximum_output_tokens=MAX_OUTPUT_TOKENS,
            maximum_response_bytes=262144, endpoint='https://api.openai.com/v1/responses', retries=0)

    def execute(self, target, lease, journal):
        from wahojobs.opportunity_enrichment import enrich_canonical_opportunity
        from wahojobs.opportunity_llm import OpenAIStructuredEnrichmentClient
        if self.journal_root != journal.path.parent.resolve():
            raise ValueError('maintenance_enrichment_journal_mismatch')
        with read_connection(target) as connection:
            plan = self.inspect(connection)
        if plan['blocked']:
            return dict(status='awaiting_authorized_repair', reasons=plan['blocked'])
        adapter = self
        class RecordedClient:
            provider, model, prompt_version = adapter.client.provider, adapter.client.model, adapter.client.prompt_version
            def enrich(self, packet):
                claims = adapter.journal_root/'enrichment-attempts'
                claims.mkdir(exist_ok=True)
                save_json(claims/(plan['attempt_key']+'.json'), dict(plan_id=journal.path.name,
                    reservation=plan['reservation'], identity=plan['identity'], status='reserved_no_automatic_retry'))
                journal.append('enrichment_request', dict(scope=plan, packet=packet))
                sent = False
                def dispatch():
                    nonlocal sent
                    if sent:
                        raise ValueError('maintenance_enrichment_secondary_dispatch_forbidden')
                    journal.append('enrichment_dispatch', dict(attempt_key=plan['attempt_key'], physical_attempts=1))
                    sent = True
                return OpenAIStructuredEnrichmentClient.enrich(adapter.client, packet,
                    max_response_bytes=plan['maximum_response_bytes'], before_dispatch=dispatch,
                    partial_response_sink=lambda raw: journal.append('enrichment_partial_response',
                        dict(raw_response=raw, capture_complete=False)),
                    response_sink=lambda raw: journal.append('enrichment_response', dict(raw_response=raw)))
        with local_inventory_connection(target, ownership=lease) as connection, connection:
            result = enrich_canonical_opportunity(connection, self.canonical_id, ensure_schema=False,
                                                   llm_client=RecordedClient())
        with read_connection(target) as connection:
            after = self.inspect(connection)
        status = 'completed' if not after['needed'] and result['llm']['outcome'] != 'failed' else 'awaiting_authorized_repair'
        return dict(status=status, result=result, after=after)


def build_plan(database, providers, *, now=None, http_limit=None, detail_limit=0,
               details='needed', phase='all', owner=None, transport_binding='production', enrichment=None):
    now = utc(now or clock_now())
    target = local_database_path(database)
    providers = list(dict.fromkeys(providers))
    if (not providers or any(p not in PROVIDERS for p in providers)
            or details not in ('needed', 'all', None) or phase not in ('all', 'source', 'derived')):
        raise ValueError('invalid_maintenance_selection')
    if (http_limit is not None and (type(http_limit) is not int or not 0 <= http_limit <= MAX_HTTP_TRANSACTIONS)
            or type(detail_limit) is not int or not 0 <= detail_limit <= MAX_DETAIL_REQUESTS):
        raise ValueError('invalid_refresh_budget')
    config = dict(providers=providers, http_limit=http_limit, detail_limit=detail_limit, details=details,
                  phase=phase, transport_binding=transport_binding)
    states, contracts, operations = [], [], []
    with read_connection(target) as connection:
        connection.execute('BEGIN')
        schema = schema_fingerprint(connection)
        for slug in providers:
            state = inspect_source(connection, slug, now)
            states.append(state)
            try:
                contract = inspect_refresh(target, [slug], details=details)['sources'][0]
            except ValueError:
                contract = dict(source=slug, unavailable='source_configuration_unavailable')
            contracts.append(contract)
            failed = state.get('latest_run') and state['latest_run']['status'] not in (
                ('success', 'partial') if slug == 'mercor' else ('success',))
            stale = [j['job_id'] for j in state['jobs'] if j['verification']['status'] in ('stale_source','unverified_source','unavailable')]
            missing = [j['job_id'] for j in state['jobs'] if j['verification']['status'] != 'inactive'
                       and j['description'] in ('missing_accepted_body', 'catalog_only_not_full_detail')]
            need_catalog = not state['jobs'] or bool(stale) or bool(failed) or bool(details and missing) or details == 'all'
            if phase != 'derived' and need_catalog:
                blocks = []
                if state['input_status'] != 'available' or 'unavailable' in contract:
                    blocks.append('source_inputs_unavailable')
                if http_limit is None or http_limit == 0:
                    blocks.append('execution_budget_absent')
                operations.append(dict(id='catalog:' + slug, kind='catalog_observation', provider=slug,
                    reasons=dict(stale_or_unverified_ids=stale, detail_gap_ids=missing,
                                 no_inventory=not state['jobs'], unsuccessful_latest=bool(failed),
                                 explicit_detail_recheck=details == 'all'),
                    prerequisites=['offline_database_ownership', 'explicit_source_authorization'],
                    blocked=blocks, request_scope=contract,
                    details=details if slug == 'alignerr' else None,
                    components=([dict(kind='supported_detail_recovery', mode=details,
                        selection='Only identity-validated records returned in this new catalog observation',
                        prerequisites=['accepted catalog authority', 'remaining detail and total HTTP allowance'],
                        writes=['content-only accepted detail and its canonical/deterministic effects'],
                        timeout_seconds=25, response_bytes=2000000)]
                        if details and slug == 'alignerr' else []),
                    detail_status=('pending_detail_budget' if details and not detail_limit and slug == 'alignerr' else 'bounded'),
                    writes=['provider jobs/captures/acceptance/crawl runs/events',
                            'provider canonical rollup', 'deterministic enrichment'],
                    fingerprint=state['fingerprint']))
            if missing and slug == 'mercor':
                operations.append(dict(id='detail:mercor', kind='detail_recovery', provider=slug,
                    reasons=dict(detail_gap_ids=missing), blocked=['no_supported_exact_detail_capability'],
                    prerequisites=['future accepted catalog body'], writes=[], request_scope=None))
            stale_derived = [e['canonical_id'] for e in state['enrichments'] if e['freshness'] != 'current'
                             and e['clause_binding_status'] not in ('stale', 'invalid')]
            if phase != 'source' and stale_derived and not any(o['id'] == 'catalog:' + slug for o in operations):
                operations.append(dict(id='enrichment:' + slug, kind='deterministic_repair', provider=slug,
                    canonical_ids=stale_derived, reasons=['source_input_or_derivation_not_current'],
                    prerequisites=['offline_database_ownership', 'explicit_derived_authorization'],
                    blocked=[], request_scope=None, writes=['selected deterministic opportunity enrichment'],
                    fingerprint=state['fingerprint']))
            for item in state['enrichments']:
                if item['clause_binding_status'] in ('stale', 'invalid'):
                    operations.append(dict(id='materiality:' + str(item['canonical_id']), kind='materiality_repair',
                        provider=slug, canonical_ids=[item['canonical_id']], reasons=item['stale_reasons'],
                        prerequisites=['existing single-canonical enrichment client and explicit model authorization'],
                        blocked=['model_materiality_repair_not_configured'], writes=['source-bound enrichment only'],
                        request_scope=dict(maximum_canonical_opportunities=1, retries=0)))
        if enrichment is not None and enrichment.canonical_id not in {
                e['canonical_id'] for s in states for e in s['enrichments']}:
            raise ValueError('maintenance_enrichment_outside_provider_scope')
        enrichment_plan = enrichment.inspect(connection) if enrichment is not None else None
        if enrichment_plan is not None:
            if enrichment_plan['provider'] not in providers:
                raise ValueError('maintenance_enrichment_outside_provider_scope')
            operations = [o for o in operations if o['id'] != 'materiality:' + str(enrichment.canonical_id)]
            if enrichment_plan['needed']:
                blocks = list(enrichment_plan['blocked'])
                if any(o['kind'] == 'catalog_observation' for o in operations):
                    blocks.append('reinspect_enrichment_binding_after_source_operation')
                operations.append(dict(id='materiality:' + str(enrichment.canonical_id), kind='materiality_repair',
                    provider=enrichment_plan['provider'], canonical_ids=[enrichment.canonical_id],
                    reasons=['explicit selective source-bound derivation preparation/repair'],
                    prerequisites=['offline_database_ownership', 'explicit_model_authorization'],
                    blocked=blocks, request_scope=enrichment_plan, writes=['selected source-bound enrichment and existing run accounting']))
        connection.rollback()
    owner_plan = None
    if owner is not None:
        # Check exact jobs before any private-profile inspection.
        available_ids = {j['job_id'] for s in states for j in s['jobs']}
        if not set(owner.job_ids) <= available_ids:
            raise ValueError('maintenance_owner_jobs_outside_provider_scope')
        try:
            owner_plan = owner.inspect()
            items = owner_plan['items']
            pending = any(i['state'] in ('needs_preparation', 'limitation') for i in items)
            if phase != 'source' and pending:
                blocks = []
                if any(o['kind'] == 'catalog_observation' for o in operations):
                    blocks.append('reinspect_owner_binding_after_source_operation')
                if owner_plan['budget'] is None:
                    blocks.append('preparation_budget_absent')
                if not owner_plan['execution_enabled']:
                    blocks.append('preparation_execution_disabled')
                if not owner_plan['store_path']:
                    blocks.append('durable_preparation_store_required')
                if not any(i['state'] == 'needs_preparation' for i in items):
                    blocks.append('existing_attempt_invalid_or_revoked_requires_separate_review')
                operations.append(dict(id='owner_preparation', kind='owner_preparation',
                    reasons=['current owner/source request requires preparation or has a limitation'],
                    prerequisites=['current session/CSRF and exact owner/profile', 'explicit_preparation_authorization',
                                   'configured existing preparer and budget', 'durable companion store'],
                    blocked=blocks, request_scope=dict(model=owner_plan['model'], basis=owner_plan['basis'],
                        job_ids=owner.job_ids, maximum_pairs=8, retries=0),
                    writes=['owner-bound companion attempts/results/generation'], expected_plan_id=owner_plan['plan_id']))
        except ValueError:
            owner_plan = dict(status='authorization_or_storage_unavailable', owner=list(owner.owner),
                              profile_id=owner.profile_id, job_ids=owner.job_ids)
            operations.append(dict(id='owner_preparation', kind='owner_preparation',
                blocked=['owner_authorization_or_storage_unavailable'], writes=[], request_scope=None))
    result = dict(version=VERSION, database=database_identity(target), created_at=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(), config=config,
        contract_fingerprint=contract_fingerprint(), sources=states, contracts=contracts,
        schema_fingerprint=schema,
        enrichment_scope=enrichment_plan,
        owner_scope=owner_plan, operations=operations, read_only=True, network_requests=0, model_requests=0,
        lifecycle='Stop only the selected database owner normally; use the supported durable launcher after maintenance. Never delete locks or kill unknown processes.',
        recovery='One execution per plan. Started or uncertain operations are never replayed. Inspect and create a fresh plan; catalog snapshots are never stitched.',
        authorization='Plan creation grants no execution authority; --execute/--yes and separate source/derived/model grants are required.')
    result['plan_id'] = digest(result)
    return result


def save_json(path, value):
    path = Path(path)
    with path.open('xb') as stream:
        stream.write(encoded(value) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


class Journal:
    def __init__(self, root, plan):
        self.path = Path(root) / plan['plan_id']
        self.path.mkdir(parents=True, exist_ok=False)
        save_json(self.path/'plan.json', plan)
        self.sequence, self.previous = 0, None

    def append(self, event, data):
        data = dict(data)
        raw = data.pop('raw_response', None)
        self.sequence += 1
        if raw is not None:
            raw_path = self.path / f'{self.sequence:06d}.raw'
            with raw_path.open('xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            data['raw_response'] = dict(file=raw_path.name, sha256=sha256(raw).hexdigest(), bytes=len(raw))
        item = dict(sequence=self.sequence, previous=self.previous, event=event, data=data)
        item['hash'] = digest(item)
        save_json(self.path/f'{self.sequence:06d}.json', item)
        self.previous = item['hash']
        return item


def report(root, plan_id):
    if not isinstance(plan_id, str) or len(plan_id) != 64 or any(c not in '0123456789abcdef' for c in plan_id):
        raise ValueError('invalid_maintenance_plan_id')
    path = Path(root)/plan_id
    plan = json.loads((path/'plan.json').read_text(encoding='utf-8'))
    _validate_plan(plan)
    previous, events = None, []
    for index, p in enumerate(sorted(path.glob('[0-9]*.json')), 1):
        item = json.loads(p.read_text(encoding='utf-8'))
        body = {k:v for k,v in item.items() if k != 'hash'}
        if (item['sequence'] != index or item['previous'] != previous or digest(body) != item['hash']):
            raise ValueError('maintenance_journal_integrity_failed')
        raw = item['data'].get('raw_response')
        if raw is not None:
            if raw['file'] != f'{index:06d}.raw' or sha256((path/raw['file']).read_bytes()).hexdigest() != raw['sha256']:
                raise ValueError('maintenance_response_integrity_failed')
        events.append(item)
        previous = item['hash']
    terminal = [e for e in events if e['event'] == 'finished']
    status = terminal[-1]['data']['status'] if terminal else 'interrupted'
    return dict(plan_id=plan_id, status=status, events=events, plan=plan,
        recovery=('No replay. Inspect current inputs and create a new explicitly authorized plan. '
                  'A started catalog requires a fresh observation; retain every original run/response. '
                  'Durable preparation attempts remain consumed; invalid/attempted results are limitations.'))


def _validate_plan(plan):
    if (plan.get('version') != VERSION or plan.get('plan_id') != digest({k:v for k,v in plan.items() if k != 'plan_id'})):
        raise ValueError('maintenance_plan_integrity_failed')


def execute_plan(plan, root, *, authorized=False, authorize_sources=False,
                 authorize_derived=False, authorize_preparation=False, owner=None, now=None,
                 transport_binding='production', enrichment=None, authorize_enrichment=False):
    _validate_plan(plan)
    if (any(type(v) is not bool for v in (authorized, authorize_sources, authorize_derived,
                                         authorize_preparation, authorize_enrichment)) or not authorized):
        raise ValueError('explicit_maintenance_execution_required')
    if (Path(root)/plan['plan_id']).exists():
        return report(root, plan['plan_id'])
    target = local_database_path(plan['database']['path'])
    from wahojobs.database_lifetime_ownership import (
        acquire_database_lifetime_ownership, release_database_lifetime_ownership, ROLE_OFFLINE_OPERATOR)
    # Ownership covers preflight through final receipts, not just each provider.
    lease = acquire_database_lifetime_ownership(target, role=ROLE_OFFLINE_OPERATOR)
    try:
        now = utc(now or clock_now())
        if (transport_binding != plan['config']['transport_binding']
                or database_identity(target) != plan['database'] or contract_fingerprint() != plan['contract_fingerprint']
                or not datetime.fromisoformat(plan['created_at']) <= now <= datetime.fromisoformat(plan['expires_at'])):
            raise ValueError('maintenance_plan_obsolete')
        with read_connection(target) as connection:
            if schema_fingerprint(connection) != plan['schema_fingerprint']:
                raise ValueError('maintenance_plan_schema_changed')
            if any(source_fingerprint(connection, s['provider']) != s['fingerprint'] for s in plan['sources']):
                raise ValueError('maintenance_plan_source_changed')
        if authorize_preparation and plan['owner_scope'] is not None and owner is not None:
            if digest(owner.inspect()) != digest(plan['owner_scope']):
                raise ValueError('maintenance_plan_owner_or_configuration_changed')
        elif plan['owner_scope'] is not None and authorize_preparation:
            raise ValueError('maintenance_owner_scope_required')
        if authorize_enrichment and plan.get('enrichment_scope') is not None:
            if enrichment is None and authorize_enrichment:
                raise ValueError('maintenance_enrichment_configuration_required')
            if enrichment is not None:
                with read_connection(target) as connection:
                    if digest(enrichment.inspect(connection)) != digest(plan['enrichment_scope']):
                        raise ValueError('maintenance_enrichment_plan_changed')
        # A JSON checksum detects damage but is not an authorization signature.
        # Reconstruct every operation that could run from the current contracts,
        # rather than trusting editable operation dictionaries or widened IDs.
        rebuilt = build_plan(target, now=datetime.fromisoformat(plan['created_at']),
            owner=owner if authorize_preparation else None,
            enrichment=enrichment if authorize_enrichment else None, **plan['config'])
        executable_kinds = set()
        if authorize_sources: executable_kinds.add('catalog_observation')
        if authorize_derived: executable_kinds.add('deterministic_repair')
        if authorize_preparation: executable_kinds.add('owner_preparation')
        if authorize_enrichment: executable_kinds.add('materiality_repair')
        expected = [o for o in rebuilt['operations'] if o['kind'] in executable_kinds]
        supplied = [o for o in plan['operations'] if o['kind'] in executable_kinds]
        if digest(expected) != digest(supplied):
            raise ValueError('maintenance_plan_operations_changed')
        pin_journal(target, root)
        journal = Journal(root, plan)
        outcomes = []
        with refresh_request_budget(sources=[o['provider'] for o in plan['operations'] if o.get('details')],
                http_limit=plan['config']['http_limit'] or 0, detail_limit=plan['config']['detail_limit'],
                audit_sink=lambda event: journal.append('source_transport', event)) as budget:
            for operation in plan['operations']:
                kind, oid = operation['kind'], operation['id']
                grant = (authorize_sources if kind == 'catalog_observation' else
                         authorize_derived if kind == 'deterministic_repair' else
                         authorize_preparation if kind == 'owner_preparation' else
                         authorize_enrichment if kind == 'materiality_repair' else False)
                blocks = list(operation.get('blocked', []))
                if not grant:
                    blocks.append('execution_authorization_absent')
                if blocks:
                    outcomes.append(dict(operation=oid, status='blocked', reasons=blocks))
                    journal.append('blocked', outcomes[-1])
                    continue
                journal.append('started', dict(operation=oid, kind=kind))
                try:
                    if kind == 'catalog_observation':
                        _, summary = run_crawl(operation['provider'], db_path=target,
                            details=operation['details'], ownership=lease)
                        budget.finish_source(operation['provider'])
                        with read_connection(target) as connection:
                            after = inspect_source(connection, operation['provider'], now)
                        status = 'completed' if summary.snapshot_complete else 'partially_completed'
                        if after['latest_run']['status'] == 'failed':
                            status = 'failed'
                        counts = budget.source_details.get(operation['provider'], {}).get('recovery') or {}
                        if any(counts.get(k, 0) for k in ('pending', 'failed', 'held')):
                            status = 'partially_completed'
                        result = dict(summary=asdict(summary), after=after, detail_counts=counts)
                    elif kind == 'deterministic_repair':
                        with local_inventory_connection(target, ownership=lease) as connection, connection:
                            result = enrich_selected_opportunities(connection, set(operation['canonical_ids']), llm_client=None)
                        with read_connection(target) as connection:
                            remaining = inspect_source(connection, operation['provider'], now)
                        unresolved = [e for e in remaining['enrichments'] if e['canonical_id'] in operation['canonical_ids'] and e['freshness'] != 'current']
                        status = 'awaiting_authorized_repair' if unresolved else 'completed'
                        result = dict(result=result, unresolved=unresolved)
                    elif kind == 'materiality_repair':
                        result = enrichment.execute(target, lease, journal)
                        status = result['status']
                    else:
                        result = owner.execute(operation['expected_plan_id'], journal)
                        result = {k: v for k, v in result.items() if k != 'items'} | {
                            'items': [{k:v for k,v in i.items() if k not in ('model_input','clause')} for i in result['items']]}
                        status = ('completed' if all(i['state'] in ('published','reusable') or i['state'] == 'skipped' and i.get('reason') in
                            ('no_supported_professional_clause','no_confirmed_role_or_supported_bound_span','deterministic_relevance_available')
                                                    for i in result['items']) else 'awaiting_authorized_repair')
                    outcomes.append(dict(operation=oid, status=status, result=result))
                    journal.append('operation_result', outcomes[-1])
                except Exception as exc:
                    # Preserve uncertainty; never retry. Do not log credential-bearing exceptions.
                    outcomes.append(dict(operation=oid, status='failed', error_type=type(exc).__name__))
                    journal.append('operation_result', outcomes[-1])
                    if operation.get('provider'):
                        budget.finish_source(operation['provider'])
            states = {o['status'] for o in outcomes}
            status = ('completed' if not states or states == {'completed'} else
                      'blocked' if states == {'blocked'} else
                      'failed' if states == {'failed'} else
                      'awaiting_authorized_repair' if 'awaiting_authorized_repair' in states or any(o['operation'] == 'owner_preparation' and o['status'] != 'completed' for o in outcomes) else
                      'partially_completed')
            journal.append('finished', dict(status=status, outcomes=[dict(operation=o['operation'], status=o['status']) for o in outcomes],
                                            request_usage=budget.summary()))
        return report(root, plan['plan_id'])
    finally:
        release_database_lifetime_ownership(lease, role=ROLE_OFFLINE_OPERATOR, database_path=target)

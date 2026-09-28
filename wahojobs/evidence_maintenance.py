"""Explicit existing-source maintenance. No scheduler or implicit model work.

Plans and receipts are local operator artifacts, not database source authority.
Production acceptance, lifecycle, preparation and storage contracts remain owners
of every evidence mutation. Journal files are append-only, exclusive and fsynced.
"""
from contextlib import contextmanager, closing
from collections import Counter
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
STAGED_BASELINE = 'staged_publication_baseline_v1'
from wahojobs.daily_source_policy import CORE_SOURCES, POLICY, daily_source
PROVIDERS = CORE_SOURCES
ROOT = Path(__file__).resolve().parents[1]


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return sha256(encoded(value)).hexdigest()


def clock_now():
    return datetime.now(timezone.utc)


FAILURE_REASONS = frozenset({'worker_execution_deadline_expired','execution_deadline_expired',
    'publication_deadline_expired','mindrift_count_drop','recovery_snapshot_file_limit',
    'recovery_source_changed','recovery_snapshot_integrity_failed','recovery_journal_unavailable',
    'no_completed_observations_to_publish'})


def failure_diagnostic(error, *, phase):
    """Bounded locations and known reasons only; no exception/response contents."""
    import traceback
    from wahojobs.tracking.service import MindriftCountDropRejected
    reason = ('mindrift_count_drop' if isinstance(error, MindriftCountDropRejected) else
        str(error) if isinstance(error,(TimeoutError,ValueError)) and str(error) in FAILURE_REASONS else None)
    return dict(phase=phase, error_type=type(error).__name__, reason=reason,
        frames=[dict(file=Path(frame.filename).name, function=frame.name, line=frame.lineno)
            for frame in traceback.extract_tb(error.__traceback__)[-8:]])


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
        from wahojobs.storage_relocation import LINEAGE, sidecar, relocation_binding
        if not sidecar(target, LINEAGE).exists():
            raise ValueError('maintenance_journal_database_identity_changed')
        return relocation_binding(target, binding)
    return binding


def pin_journal(target, root):
    """Database-local durable recovery location; never silently replace it."""
    expected = dict(database=database_identity(target), journal_root=str(Path(root).resolve()))
    from wahojobs.storage_relocation import LINEAGE, sidecar, relocation_binding
    if sidecar(target, LINEAGE).exists() and relocation_binding(target) != expected:
        raise ValueError('maintenance_journal_root_changed')
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
    return _source_fingerprint_digest(material)


def _source_fingerprint_digest(material):
    # A source footprint consists of company metadata and ordered table rows.
    # Encode one row at a time with the C JSON encoder: Python iterencode walks
    # every field in the growing history and consumes the publication window.
    # Keep the exact canonical byte stream without a source-wide JSON/UTF-8 copy,
    # omitting rows or trusting a previously stored hash in place of their bytes.
    fingerprint = sha256()
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    def value(item):
        fingerprint.update(encoder.encode(item).encode('utf-8'))
    def rows(items):
        fingerprint.update(b'[')
        for index, item in enumerate(items):
            if index: fingerprint.update(b',')
            value(item)
        fingerprint.update(b']')
    if isinstance(material, dict) and all(isinstance(key, str) for key in material):
        fingerprint.update(b'{')
        for index, key in enumerate(sorted(material)):
            if index: fingerprint.update(b',')
            value(key)
            fingerprint.update(b':')
            if isinstance(material[key], list): rows(material[key])
            else: value(material[key])
        fingerprint.update(b'}')
    elif isinstance(material, list):
        rows(material)
    else:
        # Preserve normal JSON key conversion/error behavior for other inputs.
        value(material)
    return fingerprint.hexdigest()


def _staged_baseline_evidence(connection, job, source):
    """Comparison fields only; never an accepted-history or freshness proof.

    Current material is still canonical and hashed exactly as the full reader,
    including legacy rows. Historical acceptance replay is deferred to the
    mandatory all-row inspection inside the staged publication transaction.
    """
    from wahojobs.db.repository import _verify_stored_source_material, _semantic_material_hash_from_rows
    semantic = None
    if source is not None:
        _, metadata = _verify_stored_source_material(source, 'Stored publication baseline')
        semantic = _semantic_material_hash_from_rows(job, source, metadata)
    acceptance = connection.execute('SELECT accepted_capture_id,last_confirmed_at '
        'FROM job_source_content_acceptances WHERE job_id=?', (job['id'],)).fetchone()
    latest = connection.execute('SELECT id FROM job_source_content_captures WHERE job_id=? '
        'ORDER BY id DESC LIMIT 1', (job['id'],)).fetchone()
    return dict(history_validation='deferred_until_atomic_publication',
        accepted_semantic_material_sha256=semantic,
        accepted_capture_id=acceptance['accepted_capture_id'] if acceptance else None,
        last_confirmed_at=acceptance['last_confirmed_at'] if acceptance else None,
        latest_capture_id=latest['id'] if latest else None)


def inspect_source(connection, slug, now, *, catalog_only=False, staged_baseline=False):
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
        source = connection.execute('SELECT * FROM job_source_contents WHERE job_id=?', (row['id'],)).fetchone()
        evidence = (_staged_baseline_evidence(connection, row, source) if staged_baseline else
                    get_job_source_capture_evidence(connection, row['id']))
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
    # Daily catalog publication does not inspect or repair derived matching
    # material. Keep the same source authority checks and semantic hashes.
    for canonical in ([] if catalog_only else sorted({r['canonical_opportunity_id'] for r in rows if r['canonical_opportunity_id']})):
        semantic = load_semantic_input(connection, canonical)
        stored = connection.execute('SELECT * FROM opportunity_enrichments WHERE canonical_opportunity_id=?',
                                    (canonical,)).fetchone()
        enrichments.append(dict(canonical_id=canonical, **classify_enrichment_freshness(semantic, stored)))
    latest = connection.execute('SELECT * FROM crawl_runs WHERE company_id=? ORDER BY id DESC LIMIT 1',
                                (company['id'],)).fetchone()
    state = dict(provider=slug, input_status='available', jobs=jobs, enrichments=enrichments,
                latest_run=dict(latest) if latest else None, fingerprint=source_fingerprint(connection, slug))
    if staged_baseline:
        state['inspection_mode'] = STAGED_BASELINE
        state['history_validation'] = 'deferred_until_atomic_publication'
    return state


def _require_full_staged_inspection(connection, company_id, before, after):
    """Refuse a partial/invalid receipt even if a future inspector filters rows."""
    from wahojobs.source_capture import (EVIDENCE_STATE_ACCEPTED_CURRENT, EVIDENCE_STATE_STALE_LAST_KNOWN_GOOD,
        EVIDENCE_STATE_DEGRADED_LATEST, EVIDENCE_STATE_LEGACY_ACCEPTED, EVIDENCE_STATE_MISSING)
    expected = {row[0] for row in connection.execute('SELECT id FROM jobs WHERE company_id=?', (company_id,))}
    actual = [job['job_id'] for job in after['jobs']]
    allowed_evidence = {EVIDENCE_STATE_ACCEPTED_CURRENT, EVIDENCE_STATE_STALE_LAST_KNOWN_GOOD,
        EVIDENCE_STATE_DEGRADED_LATEST, EVIDENCE_STATE_LEGACY_ACCEPTED, EVIDENCE_STATE_MISSING}
    if (not connection.in_transaction or after.get('provider') != before['provider']
            or after.get('input_status') != 'available'
            or after.get('inspection_mode') == STAGED_BASELINE or after.get('history_validation')
            or len(actual) != len(set(actual)) or set(actual) != expected
            or not {job['job_id'] for job in before['jobs']} <= expected
            or any(job['evidence'].get('state') not in allowed_evidence
                   or job['evidence'].get('history_validation')
                   or job['verification'].get('status', '').startswith('invalid') for job in after['jobs'])):
        raise RuntimeError('staged_publication_full_semantic_validation_required')


def coverage_summary(state, operations):
    """Readable operator priorities over existing evidence; no new authority.

    A trusted source row still needs candidate-specific eligibility evaluation.
    Held newer observations never replace the accepted detail in these counts.
    """
    jobs = state['jobs']
    active = [j for j in jobs if j['verification']['status'] != 'inactive']
    held = [j['job_id'] for j in jobs if j['evidence'].get('latest_capture_id') is not None
            and j['evidence'].get('latest_capture_id') != j['evidence'].get('accepted_capture_id')]
    ages = [j['verification'].get('source_age_hours') for j in active]
    known_ages = [a for a in ages if isinstance(a, (int, float))]
    dates = sorted({j['evidence']['last_confirmed_at'] for j in active if j['evidence'].get('last_confirmed_at')})
    relevant = [o for o in operations if o.get('provider') == state['provider']]
    result = dict(total_postings=len(jobs), active_postings=len(active),
        verification_counts=dict(sorted(Counter(j['verification']['status'] for j in jobs).items())),
        accepted_body_counts=dict(sorted(Counter(j['description'] for j in active).items())),
        derived_freshness_counts=dict(sorted(Counter(e['freshness'] for e in state['enrichments']).items())),
        held_latest_observation_job_ids=held,
        oldest_verification_age_hours=max(known_ages) if known_ages else None,
        unknown_verification_age_count=sum(a is None for a in ages),
        oldest_accepted_confirmation_at=dates[0] if dates else None,
        next_operations=[dict(operation_id=o['id'], kind=o['kind'], blocked=o['blocked'],
            prerequisites=o.get('prerequisites',[]), writes=o.get('writes',[])) for o in relevant],
        availability_note='Source trust is availability evidence only; candidate eligibility and saved visibility are evaluated separately.')
    if state.get('inspection_mode') == STAGED_BASELINE:
        result.update(inspection_mode=STAGED_BASELINE, history_validation='deferred_until_atomic_publication')
    return result


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
               details='needed', phase='all', owner=None, transport_binding='production', enrichment=None,
               daily_discovery=False, staged_baseline=False):
    if type(daily_discovery) is not bool:
        raise ValueError('invalid_daily_discovery')
    now = utc(now or clock_now())
    target = local_database_path(database)
    providers = list(dict.fromkeys(providers))
    if (type(staged_baseline) is not bool or staged_baseline and (not daily_discovery or phase != 'source'
            or details is not None or detail_limit != 0 or len(providers) != 1 or owner is not None or enrichment is not None)):
        raise ValueError('staged_publication_baseline_scope_required')
    if (not providers or any(p not in PROVIDERS for p in providers)
            or details not in ('needed', 'all', None) or phase not in ('all', 'source', 'derived')):
        raise ValueError('invalid_maintenance_selection')
    if (http_limit is not None and (type(http_limit) is not int or not 0 <= http_limit <= MAX_HTTP_TRANSACTIONS)
            or type(detail_limit) is not int or not 0 <= detail_limit <= MAX_DETAIL_REQUESTS):
        raise ValueError('invalid_refresh_budget')
    config = dict(providers=providers, http_limit=http_limit, detail_limit=detail_limit, details=details,
                  phase=phase, transport_binding=transport_binding, daily_discovery=daily_discovery)
    if staged_baseline:
        config['staged_baseline'] = True
    states, contracts, operations = [], [], []
    with read_connection(target) as connection:
        connection.execute('BEGIN')
        schema = schema_fingerprint(connection)
        for slug in providers:
            state = inspect_source(connection, slug, now,
                catalog_only=daily_discovery and phase=='source' and details is None
                    and owner is None and enrichment is None,
                staged_baseline=staged_baseline)
            states.append(state)
            try:
                contract = inspect_refresh(target, [slug], details=details)['sources'][0]
            except ValueError:
                contract = dict(source=slug, unavailable='source_configuration_unavailable')
            contracts.append(contract)
            failed = state.get('latest_run') and state['latest_run']['status'] not in (
                ('success', 'partial') if slug in ('mercor', 'dataannotation', 'dataforce', 'surge') else ('success',))
            stale = [j['job_id'] for j in state['jobs'] if j['verification']['status'] in ('stale_source','unverified_source','unavailable')]
            missing = [j['job_id'] for j in state['jobs'] if j['verification']['status'] != 'inactive'
                       and j['description'] in ('missing_accepted_body', 'catalog_only_not_full_detail')]
            need_catalog = daily_discovery or not state['jobs'] or bool(stale) or bool(failed) or bool(details and missing) or details == 'all'
            if phase != 'derived' and need_catalog:
                blocks = []
                if state['input_status'] != 'available' or 'unavailable' in contract:
                    blocks.append('source_inputs_unavailable')
                if POLICY[slug]['readiness'] != 'ready':
                    blocks.append(POLICY[slug]['blocker'])
                if POLICY[slug]['cooldown_hours']:
                    last=connection.execute("SELECT cr.started_at FROM crawl_runs cr JOIN companies c ON c.id=cr.company_id WHERE c.slug=? AND cr.status='success' AND cr.used_sample_data=0 AND cr.error_message IS NULL ORDER BY cr.started_at DESC,cr.id DESC LIMIT 1",(slug,)).fetchone()
                    if last and now < utc(datetime.fromisoformat(last[0]))+timedelta(hours=POLICY[slug]['cooldown_hours']):
                        blocks.append('source_success_cooldown')
                if http_limit is None or http_limit == 0:
                    blocks.append('execution_budget_absent')
                operations.append(dict(id='catalog:' + slug, kind='catalog_observation', provider=slug,
                    reasons=dict(daily_discovery=daily_discovery, stale_or_unverified_ids=stale, detail_gap_ids=missing,
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
    for state in states:
        state['coverage'] = coverage_summary(state, operations)
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
    import tempfile
    path = Path(path)
    # Never expose a partly written numbered journal entry. Hard-link creation
    # is atomic and preserves exclusive/no-overwrite semantics on both hosts.
    descriptor,pending=tempfile.mkstemp(prefix='.'+path.name+'.',suffix='.pending',dir=path.parent)
    try:
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(encoded(value) + b'\n')
            stream.flush();os.fsync(stream.fileno())
        os.link(pending,path)
        if os.name=='posix':
            directory=os.open(path.parent,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:
        os.unlink(pending)


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


def committed_publication_report(publication, database):
    """Read-only reporting view of an exact prepared, committed transaction.

    Original journals remain immutable. A prepared file alone is never proof
    of commit; the exact terminal crawl row is part of the SQLite transaction.
    """
    prepared=[e for e in publication['events'] if e['event']=='catalog_commit_prepared']
    if not prepared:return publication
    if len(prepared)!=1:raise ValueError('single_prepared_catalog_receipt_required')
    entry=prepared[0];proof=entry['data'];plan=publication['plan'];provider=proof['provider']
    links=[e['data'] for e in publication['events'] if e['event']=='staged_observation']
    if (plan['database']!=database_identity(Path(database)) or plan['config']['providers']!=[provider]
            or proof['operation']!='catalog:'+provider or len(links)!=1
            or links[0]['collection_plan_id']!=proof['collection_plan_id']
            or links[0]['collection_journal_hash']!=proof['collection_journal_hash']
            or proof['result']['after']['provider']!=provider
            or proof['result']['after']['latest_run']!=proof['crawl_run']
            or proof['crawl_run']['status'] not in ('success','partial','contract_drift')):
        raise ValueError('prepared_catalog_receipt_binding_invalid')
    with read_connection(database) as connection:
        row=connection.execute('SELECT * FROM crawl_runs WHERE id=?',(proof['crawl_run']['id'],)).fetchone()
        company=connection.execute('SELECT id FROM companies WHERE slug=?',(provider,)).fetchone()
    if row is None or company is None or dict(row)!=proof['crawl_run'] or row['company_id']!=company['id']:
        return dict(publication,prepared_commit_verified=False)
    # Build a reporting view, never append a forged historical journal event.
    result=dict(operation=proof['operation'],status='completed' if row['status']=='success' else 'partially_completed',
        result=proof['result'])
    events=[e for e in publication['events'] if e['event']!='finished' and not (
        e['event']=='operation_result' and e['data'].get('operation')==proof['operation'])]
    events.extend([dict(event='operation_result',data=result),dict(event='finished',data=dict(
        status=result['status'],request_usage=dict(http_transactions=0)))])
    return dict(publication,events=events,status=result['status'],prepared_commit_verified=True,
        reconciliation_proof=dict(prepared_event_hash=entry['hash'],crawl_run_id=row['id'],
            terminal_crawl_row_sha256=digest(dict(row))))


def _validate_plan(plan):
    if (plan.get('version') != VERSION or plan.get('plan_id') != digest({k:v for k,v in plan.items() if k != 'plan_id'})):
        raise ValueError('maintenance_plan_integrity_failed')


def _execute_plan(plan, root, *, authorized=False, authorize_sources=False,
                 authorize_derived=False, authorize_preparation=False, owner=None, now=None,
                 transport_binding='production', enrichment=None, authorize_enrichment=False, ownership=None,
                 observation=None, authorize_controlled_publication=False):
    _validate_plan(plan)
    baseline = plan['config'].get('staged_baseline', False)
    if (type(baseline) is not bool or baseline and (observation is None or ownership is None
            or not authorize_sources or authorize_derived or authorize_preparation or authorize_enrichment
            or owner is not None or enrichment is not None or plan['config'].get('daily_discovery') is not True
            or plan['config'].get('phase') != 'source' or plan['config'].get('details') is not None
            or plan['config'].get('detail_limit') != 0 or len(plan['config']['providers']) != 1)):
        raise ValueError('staged_publication_baseline_requires_atomic_observation')
    if authorize_controlled_publication and (observation is None or not observation.controlled_validation
            or plan['config']['providers'][0] not in ('dataannotation', 'dataforce')):
        raise ValueError('controlled_publication_requires_exact_source_observation')
    if observation is not None:
        from wahojobs.crawler.staged_observation import validate_observation
        if (len(plan['config']['providers']) != 1 or ownership is None or not authorize_sources
                or authorize_derived or authorize_preparation or authorize_enrichment
                or plan['config'].get('details') is not None):
            raise ValueError('catalog_only_staged_publication_required')
        validate_observation(observation, plan['config']['providers'][0])
    if (any(type(v) is not bool for v in (authorized, authorize_sources, authorize_derived,
                                         authorize_preparation, authorize_enrichment)) or not authorized):
        raise ValueError('explicit_maintenance_execution_required')
    if (Path(root)/plan['plan_id']).exists():
        return report(root, plan['plan_id'])
    target = local_database_path(plan['database']['path'])
    from wahojobs.database_lifetime_ownership import (
        acquire_database_lifetime_ownership, release_database_lifetime_ownership, ROLE_OFFLINE_OPERATOR)
    # Ownership covers preflight through final receipts, not just each provider.
    from wahojobs.database_lifetime_ownership import require_database_lifetime_ownership
    lease = ownership or acquire_database_lifetime_ownership(target, role=ROLE_OFFLINE_OPERATOR)
    require_database_lifetime_ownership(lease, role=ROLE_OFFLINE_OPERATOR, database_path=target)
    try:
        now = utc(now or clock_now())
        if (transport_binding != plan['config']['transport_binding']
                or database_identity(target) != plan['database'] or contract_fingerprint() != plan['contract_fingerprint']
                or not datetime.fromisoformat(plan['created_at']) <= now <= datetime.fromisoformat(plan['expires_at'])):
            raise ValueError('maintenance_plan_obsolete')
        with read_connection(target) as connection:
            if schema_fingerprint(connection) != plan['schema_fingerprint']:
                raise ValueError('maintenance_plan_schema_changed')
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
        # The independently rebuilt plan already scans every source row and all
        # capture history in its read transaction. Bind that fresh full proof to
        # the supplied plan instead of hashing the same growing history again
        # immediately before rebuilding. The hash format and inspected material
        # remain unchanged; malformed, missing or duplicate scopes fail closed.
        if rebuilt['schema_fingerprint'] != plan['schema_fingerprint']:
            raise ValueError('maintenance_plan_schema_changed')
        supplied_sources = plan.get('sources')
        rebuilt_sources = rebuilt['sources']
        if (type(supplied_sources) is not list
                or any(type(source) is not dict or not isinstance(source.get('provider'), str)
                       or not isinstance(source.get('fingerprint'), str) for source in supplied_sources)):
            raise ValueError('maintenance_plan_source_changed')
        supplied_fingerprints = {source['provider']: source['fingerprint'] for source in supplied_sources}
        rebuilt_fingerprints = {source['provider']: source['fingerprint'] for source in rebuilt_sources}
        if (len(supplied_fingerprints) != len(supplied_sources)
                or len(rebuilt_fingerprints) != len(rebuilt_sources)
                or set(rebuilt_fingerprints) != set(plan['config']['providers'])
                or supplied_fingerprints != rebuilt_fingerprints):
            raise ValueError('maintenance_plan_source_changed')
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
        if observation is not None:
            journal.append('staged_observation', dict(collection_plan_id=observation.collection_plan_id,
                collection_journal_hash=observation.journal_hash, source=observation.source,
                started_at=observation.started_at, completed_at=observation.completed_at,
                publication_started_at=clock_now().isoformat()))
        outcomes = []
        with refresh_request_budget(sources=[o['provider'] for o in plan['operations'] if o.get('details')],
                http_limit=0 if observation is not None else plan['config']['http_limit'] or 0, detail_limit=plan['config']['detail_limit'],
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
                prepared=None
                failure_stage='lifecycle' if kind=='catalog_observation' else kind
                try:
                    if kind == 'catalog_observation':
                        def prepare_commit(connection, company, crawl_run_id, summary):
                            nonlocal prepared, failure_stage
                            failure_stage='receipt_preparation'
                            after=inspect_source(connection,operation['provider'],now,
                                catalog_only=plan['config'].get('daily_discovery') is True
                                    and plan['config']['phase']=='source' and plan['config']['details'] is None
                                    and plan['owner_scope'] is None and plan['enrichment_scope'] is None,
                                staged_baseline=False)
                            if baseline:
                                _require_full_staged_inspection(connection, company['id'], plan['sources'][0], after)
                            terminal=dict(connection.execute('SELECT * FROM crawl_runs WHERE id=?',(crawl_run_id,)).fetchone())
                            prepared=dict(operation=oid,provider=operation['provider'],crawl_run=terminal,
                                result=dict(summary=asdict(summary),after=after,detail_counts={}),
                                collection_plan_id=observation.collection_plan_id,
                                collection_journal_hash=observation.journal_hash)
                            journal.append('catalog_commit_prepared',prepared)
                            failure_stage='transaction_commit'
                        with daily_source(operation['provider']):
                            _, summary = run_crawl(operation['provider'], db_path=target,
                                details=operation['details'], ownership=lease,
                                **({'observation': observation,
                                    'authorize_controlled_publication': authorize_controlled_publication,
                                    'before_lifecycle_commit':prepare_commit}
                                   if observation is not None else {}))
                        failure_stage='receipt_finalization'
                        budget.finish_source(operation['provider'])
                        if baseline and prepared is None:
                            raise RuntimeError('staged_publication_atomic_receipt_required')
                        if prepared is not None:after=prepared['result']['after']
                        else:
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
                    diagnostic=failure_diagnostic(exc,phase=failure_stage)
                    if kind=='catalog_observation' and diagnostic['reason']=='mindrift_count_drop':
                        diagnostic['phase']='qualification'
                    outcomes.append(dict(operation=oid, status='failed', error_type=type(exc).__name__,
                        failure_diagnostic=diagnostic,
                        **({'stage':'publication_receipt','prepared_crawl_run_id':prepared['crawl_run']['id']}
                           if prepared is not None else {})))
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
        if ownership is None:
            release_database_lifetime_ownership(lease, role=ROLE_OFFLINE_OPERATOR, database_path=target)


def execute_plan(plan, root, *, ownership=None, **options):
    # A deferred baseline is not executable through the ordinary manual path.
    # Refuse before creating even the supervisor gate's lock sidecar.
    baseline = plan.get('config', {}).get('staged_baseline', False)
    if type(baseline) is not bool or baseline and (ownership is None or options.get('observation') is None):
        _validate_plan(plan)
        raise ValueError('staged_publication_baseline_requires_atomic_observation')
    # A daily worker already holds lifetime ownership continuously across its
    # verified backup and both source plans. Ordinary manual operations also
    # share the supervisor gate, so they cannot enter its stop/start interval.
    if ownership is not None:
        return _execute_plan(plan, root, ownership=ownership, **options)
    from wahojobs.maintenance_gate import operation_gate
    with operation_gate(Path(plan['database']['path'])):
        return _execute_plan(plan, root, **options)

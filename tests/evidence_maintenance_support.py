"""Labelled offline operational fixture. Never accesses an existing app/database.

Recorded Alignerr HTML bytes are reused. Catalog envelopes, Mercor bodies,
candidate identity and clock progression are synthetic, not live observations.
Only urllib transports and test clocks are intercepted; acceptance is production.
"""
from contextlib import closing, contextmanager, ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.message import Message
import gc
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit, urlencode

from wahojobs import evidence_maintenance as maintenance
from tests.test_provider_detail_recovery import CASES, response
from tests.test_local_inventory_refresh import listing, DESCRIPTION
from tests.test_accepted_title_uncertainty import BODY, profile

T0 = datetime(2026, 9, 6, 14, tzinfo=timezone.utc)
COHORT = ('alignerr', 'mercor')
LABEL = 'SYNTHETIC offline evidence maintenance v1; simulated cycles, zero external requests'
TRANSPORT = 'labelled_offline_maintenance_fixture_v1'


def records(*, absent=True):
    base = dict(deepcopy(DESCRIPTION['record']), listingId='synthetic-maintenance-support',
                title='Customer success / support operations Evaluator', description=BODY,
                listingDomain='Customer Support', eligibleLocation=[], eligibleResidenceLocation=[])
    return [base, dict(base, listingId='synthetic-maintenance-absent', title='Secondary support evaluator')] if absent else [base]


class BytesResponse:
    def __init__(self, body, url, status=200):
        self.stream, self.url, self.status = io.BytesIO(body), url, status
        self.headers = Message()
        self.headers['Content-Type'] = 'application/json; charset=utf-8'
    def __enter__(self): return self
    def __exit__(self, *args): self.stream.close()
    def read(self, *args): return self.stream.read(*args)
    def geturl(self): return self.url


@contextmanager
def offline_transport(now, *, partial=False, pagination_failure=False, fail_mercor=False, held_detail=False, changed=False):
    calls = []
    class OfflineOpen:
        def open(self, request, timeout):
            url = request.full_url
            calls.append(dict(url=url, timeout=timeout, method=request.get_method()))
            parsed = urlsplit(url)
            if parsed.hostname == 'www.alignerr.com' and parsed.path == '/api/jobs':
                offset = int(parse_qs(parsed.query)['offset'][0])
                if offset:
                    if pagination_failure:
                        raise OSError('LABELLED offline pagination interruption')
                jobs = [] if offset else [listing(CASES[0])]
                if changed:
                    for job in jobs: job['pay'] = 'Synthetic revised terms'
                raw = json.dumps(dict(jobs=jobs, total=2 if partial or pagination_failure else 1,
                                      limit=1 if partial or pagination_failure else 120, offset=offset)).encode()
            elif parsed.hostname == 'www.alignerr.com' and parsed.path == '/jobs/' + CASES[0]['external_id']:
                raw = response(CASES[0]).body
                if held_detail:
                    raw = raw.replace(b'Biology', b'Completely Different')
            elif url == 'https://aws.api.mercor.com/work/listings-explore-page':
                if fail_mercor:
                    raise OSError('LABELLED offline Mercor failure')
                raw = json.dumps(dict(listings=records(absent=now == T0))).encode()
            else:
                raise AssertionError('Unplanned fixture URL: ' + url)
            return BytesResponse(raw, url)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz else now.replace(tzinfo=None)
    with ExitStack() as stack:
        stack.enter_context(patch('urllib.request.build_opener', return_value=OfflineOpen()))
        stack.enter_context(patch('wahojobs.crawler.provider_details.build_opener', return_value=OfflineOpen()))
        stack.enter_context(patch('wahojobs.crawler.pipeline.utc_now', return_value=now.isoformat()))
        stack.enter_context(patch('wahojobs.crawler.provider_details.datetime', FixedDatetime))
        stack.enter_context(patch('wahojobs.evidence_maintenance.clock_now', return_value=now))
        stack.enter_context(patch('socket.create_connection', side_effect=AssertionError('No external requests')))
        yield calls


def new_inventory(directory):
    from wahojobs.db.repository import initialize_database
    from tests.ai_profile_import_test_support import (
        install_ai_profile_import_database, intake_grant, import_source_metadata, NOW)
    from wahojobs.ai_profile_import import AIProfileImportService, AIProfileImportReservationRequest
    directory = Path(directory).resolve()
    if any((p/'.git').exists() for p in (directory, *directory.parents)):
        raise ValueError('demo_requires_disposable_directory_outside_checkout')
    directory.mkdir(parents=True, exist_ok=False)
    path = directory/'inventory.sqlite3'
    connection, historical_session = install_ai_profile_import_database(path)
    with closing(connection):
        # An unrelated synthetic account's historical entitlement and attempt
        # must survive catalog maintenance without even loading its profile.
        grant = intake_grant(path, historical_session)
        AIProfileImportService().reserve(connection, grant,
            AIProfileImportReservationRequest('maintenance-historical-import', import_source_metadata()), now=NOW)
    initialize_database(path)
    gc.collect()
    maintenance.save_json(directory/'synthetic.json', dict(label=LABEL, version=1, database=maintenance.database_identity(path)))
    return path


def source_plan(path, now, **options):
    return maintenance.build_plan(path, COHORT, now=now, http_limit=8, detail_limit=2,
        phase='source', transport_binding=TRANSPORT, **options)


def source_execute(path, journal, now, **scenario):
    plan = source_plan(path, now, details='all' if scenario.get('held_detail') else 'needed')
    with offline_transport(now, **scenario):
        result = maintenance.execute_plan(plan, journal, authorized=True, authorize_sources=True,
                                         now=now, transport_binding=TRANSPORT)
    return plan, result


def seed_owner(path, directory):
    from tests.google_oidc_gateway_test_support import seed_existing_google_identity
    from tests.ownership_test_support import add_principal, add_binding, add_activation_event
    from tests.candidate_continuity_support import add_candidate
    from wahojobs.accounts import create_session
    from wahojobs.professional_background_store import initialize_preparation_store
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        from tests.accounts_test_support import NOW as ACCOUNT_CREATED_AT
        user = seed_existing_google_identity(connection, suffix='maintenance-offline', created_at=ACCOUNT_CREATED_AT).user.user_id
        principal = add_principal(connection, suffix='81', environment='private_beta', principal_type='account_native',
                                  claim_policy='account_native', status='active', exclusive=1)
        binding = add_binding(connection, principal, user, suffix='81', environment='private_beta')
        add_activation_event(connection, principal, user, binding, suffix='81', environment='private_beta')
        connection.commit()
        pid = add_candidate(connection, profile('Customer support specialist', 6),
            principal_id=principal, account_id=user, now=T0, key='maintenance-offline-profile')
        session = create_session(connection, user_id=user, idle_ttl=timedelta(days=30),
            absolute_ttl=timedelta(days=30), idempotency_key='maintenance-offline-session', now=T0)
        jobs = [r[0] for r in connection.execute("SELECT id FROM jobs WHERE external_id='synthetic-maintenance-support'")]
    companion = directory/'professional-background.sqlite3'
    with closing(sqlite3.connect(companion)) as connection:
        initialize_preparation_store(connection)
    doc = dict(label=LABEL, environment='private_beta', account_id=user, principal_id=principal,
               profile_id=pid, job_ids=jobs, companion=str(companion),
               session_token=session.session_token, csrf_secret=session.csrf_secret)
    maintenance.save_json(directory/'owner-session.json', doc)
    maintenance.save_json(directory/'preparation-budget.json', dict(request_limit=2, token_limit=100000))
    return doc


def owner_for(directory, now, *, relation='supported_partial', enabled=True):
    from scripts.evidence_maintenance import owner_scope
    with patch('wahojobs.evidence_maintenance.clock_now', return_value=now):
        owner = owner_scope(directory/'inventory.sqlite3', directory/'owner-session.json',
            directory/'preparation-budget.json', enable=enabled, offline=True)
    # Services retain this stable test clock, not host time.
    owner.service._clock = lambda: now
    owner.service._authentication_gateway._clock = lambda: now
    owner.preparer._client.session.relation = relation
    return owner


def prepare_cycle(directory, now, *, relation='supported_partial'):
    owner = owner_for(directory, now, relation=relation)
    plan = maintenance.build_plan(directory/'inventory.sqlite3', COHORT, now=now, phase='derived',
                                  owner=owner, transport_binding=TRANSPORT)
    result = maintenance.execute_plan(plan, directory/'journal', authorized=True,
        authorize_derived=True, authorize_preparation=True, owner=owner, now=now, transport_binding=TRANSPORT)
    return plan, result


def offline_enrichment(canonical, journal):
    from wahojobs.opportunity_llm import OpenAIStructuredEnrichmentClient, DEFAULT_MODEL
    from wahojobs.professional_background_preparation import PreparationBudget
    from wahojobs.opportunity_enrichment import blank_llm_payload
    from tests.professional_background_preparation_support import OfflineResponse
    class Session:
        calls = 0
        interrupt = False
        def post(self, url, **kwargs):
            self.calls += 1
            if self.interrupt:
                raise KeyboardInterrupt('Labelled offline interrupted model transport')
            document = kwargs['json']
            packet = json.loads(document['input'][1]['content'][0]['text'])
            payload = blank_llm_payload()
            payload['clause_materiality'] = [dict(clause_id=c['clause_id'], classification='ambiguous')
                                              for c in packet['qualification_clauses']]
            raw = dict(id='offline-enrichment-response', model=document['model'], status='completed',
                service_tier='default', usage=dict(input_tokens=100, output_tokens=100, total_tokens=200),
                output=[dict(type='message', content=[dict(type='output_text', text=json.dumps(payload))])])
            return OfflineResponse(json.dumps(raw).encode())
    class Client(OpenAIStructuredEnrichmentClient):
        offline_labelled_stub = True
    client = Client('offline-synthetic-not-a-key', model=DEFAULT_MODEL, session=Session(), service_tier='default')
    return maintenance.EnrichmentRepair(canonical, client, PreparationBudget(1, 200000), journal)


@contextmanager
def consumer(directory, now):
    from wahojobs.workos_authkit_staging import _build_profile_integration
    from wahojobs.public_job_canary import PublicJobCanaryRoutingGate
    from wahojobs.matching.metadata_overlay import OpportunityMetadataOverlay
    owner = owner_for(directory, now, enabled=False)
    connections = SimpleNamespace(read_only_connection_provider=owner.provider,
        writable_connection_provider=lambda: maintenance.local_inventory_connection(directory/'inventory.sqlite3'))
    with (patch('wahojobs.profile_intake.openai_adapter.configured_openai_profile_adapter', return_value=None),
          patch('wahojobs.matching.metadata_overlay.load_overlay', return_value=OpportunityMetadataOverlay(directory/'none.json', {}))):
        outer = _build_profile_integration(connections, SimpleNamespace(environment_namespace='private_beta',
            public_origin='https://app.test', public_job_canary_gate=PublicJobCanaryRoutingGate.disabled()),
            lambda: now, professional_background_preparer=owner.preparer)
    try:
        yield outer, owner
    finally:
        outer.close()


def consumer_receipt(directory, now, *, actions=False):
    from tests.candidate_continuity_support import Page
    from wahojobs.authenticated_variant_details import variant_detail_url
    with consumer(directory, now) as (outer, owner):
        integration = outer._matches_integration
        headers = [('Host','app.test'), ('Cookie','wahojobs_session=' + owner.credentials['session_token']
                   + '; __Host-wahojobs_session_csrf=' + owner.credentials['csrf_secret'])]
        result = integration.handle('GET', '/find-matches', headers)
        assert result.status == 200, (result.status, result.body)
        run = next(reversed(integration._registry._runs.values()))
        matches = [m for rows in run.recommendation_context['matches'].values() for m in rows]
        selected = next((m for m in matches if m['job_id'] == owner.job_ids[0]), None)
        # Current exact-detail consumption is independent of Matches membership
        # and remains reachable for the same hidden/applied stable posting.
        from wahojobs import authenticated_variant_details as variants
        with owner.provider() as connection:
            row = connection.execute('SELECT canonical_opportunity_id FROM jobs WHERE id=?', (owner.job_ids[0],)).fetchone()
        detail_path = variant_detail_url(dict(job_id=owner.job_ids[0], canonical_opportunity_id=row[0]))
        evaluated = []
        original = variants.resolve_scoped_variant
        def observe(*args, **kwargs):
            value = original(*args, **kwargs)
            evaluated.append(value)
            return value
        with patch.object(variants, 'resolve_scoped_variant', side_effect=observe):
            detail = integration.handle('GET', detail_path, headers)
        assert detail.status == 200, (detail.status, detail.body)
        assert len(evaluated) == 1, 'Actual scoped production consumer must execute'
        job, checks = evaluated[0]
        assert checks['match']['job_id'] == owner.job_ids[0]
        if actions:
            assert detail is not None
            def submit(form):
                body = urlencode(form['fields']).encode()
                response_ = integration.handle('POST', form['target'], headers + [
                    ('Origin','https://app.test'), ('Sec-Fetch-Site','same-origin'),
                    ('Content-Type','application/x-www-form-urlencoded'), ('Content-Length',str(len(body))),
                    ('Accept','application/json')], io.BytesIO(body))
                assert response_.status == 200, (response_.status,response_.body)
                return json.loads(response_.body)
            saved = submit(Page(detail.body).action('save'))
            item = saved['pipeline_item_id']
            tracker = integration.handle('GET','/tracker',headers)
            link = next(h for h in Page(tracker.body).links if h.startswith('/tracker/item?'))
            for action in ('remind_later','applied','not_interested'):
                page = integration.handle('GET',link,headers)
                submit(Page(page.body).action(action))
        comparisons = checks['background_card_evidence']['comparisons']
        semantic = [r['components']['occupational_relevance'].get('semantic')
                    for r in comparisons if r['kind']=='professional_background']
        assert len(semantic) == 1, 'The exact professional requirement must be consumed'
        return dict(label=LABEL, matches_status=result.status, detail_status=detail.status if detail else None,
                    selected_job=owner.job_ids[0], selected_present=selected is not None, semantic=semantic,
                    source_trust=checks['match']['opportunity_trust_status'], public_state=job['public_state'],
                    model_calls=len(owner.preparer._client.session.calls))


def protected_state(path):
    source_tables = {'companies','jobs','canonical_opportunities','crawl_runs','job_events','job_source_contents',
        'job_source_content_captures','job_source_content_acceptances','opportunity_enrichments',
        'opportunity_enrichment_overrides','opportunity_enrichment_runs','opportunity_enrichment_run_diagnostics',
        'sqlite_sequence'}
    with maintenance.read_connection(path) as connection:
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                  if r[0] not in source_tables]
        return {table: [dict(r) for r in connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid')] for table in tables}


def demo_command(directory, step):
    directory = Path(directory).resolve()
    if step == 'run':
        outputs = [demo_command(directory, 'init')]
        for _ in range(2):
            outputs.extend(demo_command(directory, s) for s in ('inspect','plan','execute','report','next'))
        return dict(label=LABEL, steps=outputs, directory=str(directory))
    if step == 'init':
        path = new_inventory(directory)
        seed_plan, seed = source_execute(path, directory/'journal', T0)
        seed_owner(path, directory)
        prep, prepared = prepare_cycle(directory, T0)
        first = consumer_receipt(directory, T0, actions=True)
        assert first['semantic'][0]['relation'] == 'supported_partial'
        assert first['source_trust'] == 'trusted'
        maintenance.save_json(directory/'protected.json', protected_state(path))
        maintenance.save_json(directory/'setup.json', dict(label=LABEL, source_plan=seed_plan['plan_id'],
            preparation_plan=prep['plan_id'], consumer=first))
        maintenance.save_json(directory/'cycle-1.json', dict(now=(T0+timedelta(hours=73)).isoformat(), partial=False))
        return dict(label=LABEL, status='initialized', next='inspect', directory=str(directory))
    marker = json.loads((directory/'synthetic.json').read_text(encoding='utf-8'))
    if marker.get('label') != LABEL or marker.get('database') != maintenance.database_identity(directory/'inventory.sqlite3'):
        raise ValueError('synthetic_demo_marker_required')
    cycles = sorted(directory.glob('cycle-[0-9].json'))
    cycle_file = cycles[-1]
    cycle = int(cycle_file.stem.split('-')[1])
    setting = json.loads(cycle_file.read_text(encoding='utf-8'))
    now = datetime.fromisoformat(setting['now'])
    path = directory/'inventory.sqlite3'
    plan_path = directory/f'plan-{cycle}.json'
    if step in ('inspect','plan'):
        owner = owner_for(directory, now)
        plan = maintenance.build_plan(path, COHORT, now=now, http_limit=8, detail_limit=2,
            owner=owner, transport_binding=TRANSPORT)
        if step == 'plan':
            maintenance.save_json(plan_path, plan)
        return plan
    if step == 'execute':
        if (directory/f'cycle-{cycle}-receipt.json').exists():
            return json.loads((directory/f'cycle-{cycle}-receipt.json').read_text(encoding='utf-8'))
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        owner = owner_for(directory, now)
        before_source = consumer_receipt(directory, now)
        assert before_source['source_trust'] == 'stale_source'
        with offline_transport(now, partial=setting['partial']):
            outcome = maintenance.execute_plan(plan, directory/'journal', authorized=True,
                authorize_sources=True, owner=owner, now=now, transport_binding=TRANSPORT)
        after_source = consumer_receipt(directory, now)
        assert after_source['source_trust'] == 'trusted'
        assert after_source['semantic'] == [None], after_source
        repair_plan, repaired = prepare_cycle(directory, now)
        after_repair = consumer_receipt(directory, now)
        assert after_repair['semantic'][0]['relation'] == 'supported_partial'
        assert after_repair['semantic'][0]['request_id'] != before_source['semantic'][0]['request_id']
        assert protected_state(path) == json.loads((directory/'protected.json').read_text(encoding='utf-8'))
        receipt = dict(label=LABEL, cycle=cycle, source_status=outcome['status'], source_plan=plan['plan_id'],
            repair_status=repaired['status'], repair_plan=repair_plan['plan_id'],
            before_source=before_source, after_source=after_source, after_repair=after_repair, protected_state_equal=True)
        if not (directory/f'cycle-{cycle}-receipt.json').exists():
            maintenance.save_json(directory/f'cycle-{cycle}-receipt.json', receipt)
        return receipt
    if step == 'report':
        return json.loads((directory/f'cycle-{cycle}-receipt.json').read_text(encoding='utf-8'))
    if step == 'next':
        if cycle >= 2:
            return dict(label=LABEL, status='two_simulated_cycles_complete', directory=str(directory))
        maintenance.save_json(directory/'cycle-2.json', dict(now=(T0+timedelta(hours=146)).isoformat(), partial=True))
        return dict(label=LABEL, next='inspect', cycle=2)
    raise ValueError('invalid_demo_step')

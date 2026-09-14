"""Actual planner/executor, provider transports and consumers; disposable only."""
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from wahojobs import evidence_maintenance as m
from tests.evidence_maintenance_support import (
    new_inventory, source_execute, source_plan, offline_transport, T0, TRANSPORT,
    COHORT, seed_owner, owner_for, prepare_cycle, consumer_receipt, protected_state, demo_command,
    offline_enrichment,
)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='maintenance-v1-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)/'fixture'
        self.path = new_inventory(self.directory)
        self.journal = self.directory/'journal'

    def seed(self):
        return source_execute(self.path, self.journal, T0)

    def test_read_only_separates_budget_inputs_teaser_and_freshness(self):
        before = self.path.read_bytes()
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
            plan = m.build_plan(self.path, COHORT, now=T0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(all(s['input_status'] == 'available' for s in plan['sources']))
        self.assertTrue(all('execution_budget_absent' in o['blocked'] for o in plan['operations']))
        plan = m.build_plan(self.path, ['alignerr'], now=T0, http_limit=1, detail_limit=0)
        with offline_transport(T0):
            m.execute_plan(plan, self.journal, authorized=True, authorize_sources=True, now=T0)
        after = m.build_plan(self.path, ['alignerr'], now=T0, http_limit=1)
        self.assertEqual(after['sources'][0]['jobs'][0]['description'], 'catalog_only_not_full_detail')

    def test_two_simulated_cycles_cli_and_preservation(self):
        # Use a separate fresh path because setUp already installed this fixture.
        result = demo_command(Path(self.temp.name)/'demo', 'run')
        receipts = [s for s in result['steps'] if 'protected_state_equal' in s]
        self.assertEqual(len(receipts), 4)  # execute and report for each cycle
        self.assertTrue(all(s['protected_state_equal'] for s in receipts))
        self.assertEqual(receipts[0]['after_source']['model_calls'], 0)

    def test_alignerr_detail_binding_reused_mercor_replaced_and_absence_safe(self):
        self.seed()
        before = m.build_plan(self.path, COHORT, now=T0)
        now = T0 + timedelta(hours=73)
        plan, result = source_execute(self.path, self.journal, now)
        after = m.build_plan(self.path, COHORT, now=now)
        a_before, a_after = before['sources'][0]['jobs'][0], after['sources'][0]['jobs'][0]
        self.assertEqual(a_before['evidence']['accepted_capture_id'], a_after['evidence']['accepted_capture_id'])
        self.assertEqual(a_after['description'], 'accepted_full_detail')
        self.assertEqual(a_after['verification']['status'], 'trusted')
        mercor_before = {j['external_id']:j for j in before['sources'][1]['jobs']}
        for job in after['sources'][1]['jobs']:
            prior = mercor_before[job['external_id']]
            if job['external_id'].endswith('absent'):
                self.assertEqual(prior['evidence']['accepted_capture_id'], job['evidence']['accepted_capture_id'])
                self.assertEqual(job['verification']['status'], 'stale_source')
            else:
                self.assertNotEqual(prior['evidence']['accepted_capture_id'], job['evidence']['accepted_capture_id'])
                self.assertEqual(job['verification']['status'], 'trusted')
        self.assertEqual(result['status'], 'partially_completed')  # Mercor is always partial
        self.assertGreater(len(list((self.journal/plan['plan_id']).glob('*.raw'))), 0)

    def test_budget_pagination_failure_and_failed_observation_stay_limited(self):
        self.seed()
        now = T0 + timedelta(hours=73)
        plan, result = source_execute(self.path, self.journal, now, partial=True, fail_mercor=True)
        after = m.build_plan(self.path, COHORT, now=now)
        self.assertEqual(after['sources'][0]['latest_run']['status'], 'partial')
        self.assertEqual(after['sources'][1]['latest_run']['status'], 'failed')
        self.assertTrue(all(j['verification']['status'] == 'stale_source' for s in after['sources'] for j in s['jobs']))
        self.assertTrue(any(e['event']=='source_transport' and e['data']['event']=='transport_error' for e in result['events']))

    def test_stale_plan_binding_budget_and_authorization_no_dispatch(self):
        plan = source_plan(self.path, T0)
        with self.assertRaisesRegex(ValueError, 'explicit_maintenance'):
            m.execute_plan(plan, self.journal, now=T0)
        changed = dict(plan, config=dict(plan['config'], http_limit=100))
        with self.assertRaisesRegex(ValueError, 'integrity'):
            m.execute_plan(changed, self.journal, authorized=True, now=T0)
        with self.assertRaisesRegex(ValueError, 'obsolete'):
            m.execute_plan(plan, self.journal, authorized=True, now=T0+timedelta(hours=2))
        self.seed()
        with self.assertRaisesRegex(ValueError, 'source_changed'):
            m.execute_plan(plan, self.directory/'other', authorized=True, authorize_sources=True,
                           now=T0, transport_binding=TRANSPORT)

    def test_interrupted_execution_never_replays_and_fresh_plan_recovers(self):
        plan = source_plan(self.path, T0)
        with offline_transport(T0), patch('wahojobs.evidence_maintenance.run_crawl', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                m.execute_plan(plan, self.journal, authorized=True, authorize_sources=True,
                               now=T0, transport_binding=TRANSPORT)
        with patch('wahojobs.evidence_maintenance.run_crawl', side_effect=AssertionError('must not replay')):
            replay = m.execute_plan(plan, self.journal, authorized=True, authorize_sources=True, now=T0)
        self.assertEqual(replay['status'], 'interrupted')
        fresh, result = source_execute(self.path, self.journal, T0+timedelta(seconds=1))
        self.assertNotEqual(fresh['plan_id'], plan['plan_id'])
        self.assertEqual(result['status'], 'partially_completed')

    def test_owned_database_blocks_before_requests_and_journal(self):
        from wahojobs.database_lifetime_ownership import (
            acquire_database_lifetime_ownership, release_database_lifetime_ownership, ROLE_DURABLE_RUNTIME)
        plan = source_plan(self.path, T0)
        lease = acquire_database_lifetime_ownership(self.path, role=ROLE_DURABLE_RUNTIME)
        try:
            with self.assertRaises(Exception):
                m.execute_plan(plan, self.journal, authorized=True, authorize_sources=True,
                               now=T0, transport_binding=TRANSPORT)
        finally:
            release_database_lifetime_ownership(lease, role=ROLE_DURABLE_RUNTIME, database_path=self.path)
        self.assertFalse(self.journal.exists())

    def test_owner_repair_and_conservative_reuse_after_restart(self):
        self.seed()
        seed_owner(self.path, self.directory)
        plan, result = prepare_cycle(self.directory, T0, relation='not_established')
        self.assertEqual(result['status'], 'completed')
        owner = owner_for(self.directory, T0)
        reused = m.build_plan(self.path, COHORT, phase='derived', owner=owner, now=T0)
        self.assertEqual(reused['owner_scope']['items'][0]['state'], 'reusable')
        self.assertEqual(reused['owner_scope']['items'][0]['relation'], 'not_established')
        self.assertFalse(any(o['kind']=='owner_preparation' for o in reused['operations']))
        self.assertEqual(len(owner.preparer._client.session.calls), 0)

    def test_wrong_owner_and_missing_preparation_budget_do_not_block_source(self):
        self.seed(); seed_owner(self.path, self.directory)
        now=T0+timedelta(hours=73)
        owner=owner_for(self.directory, now)
        owner.owner=('different', *owner.owner[1:])
        plan=m.build_plan(self.path,COHORT,owner=owner,now=now,http_limit=8,detail_limit=2,
                          transport_binding=TRANSPORT)
        self.assertEqual(plan['owner_scope']['status'],'authorization_or_storage_unavailable')
        with offline_transport(now):
            result=m.execute_plan(plan,self.journal,authorized=True,authorize_sources=True,now=now,
                                  transport_binding=TRANSPORT)
        self.assertEqual(result['status'],'awaiting_authorized_repair')

    def test_selective_materiality_repair_does_not_rebind_and_reuses_conservative(self):
        self.seed()
        before = m.build_plan(self.path, COHORT, now=T0)
        canonical = next(j['canonical_id'] for j in before['sources'][1]['jobs'] if j['external_id'].endswith('support'))
        repair = offline_enrichment(canonical, self.journal)
        def plan_at(now):
            return m.build_plan(self.path, COHORT, now=now, phase='derived', enrichment=repair, transport_binding=TRANSPORT)
        plan = plan_at(T0)
        result = m.execute_plan(plan, self.journal, authorized=True, authorize_enrichment=True, enrichment=repair,
                               now=T0, transport_binding=TRANSPORT)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(repair.client.session.calls, 1)
        self.assertFalse(plan_at(T0)['enrichment_scope']['needed'])
        now = T0+timedelta(hours=73)
        source_execute(self.path, self.journal, now)
        obsolete = plan_at(now)
        self.assertEqual(obsolete['enrichment_scope']['freshness']['clause_binding_status'], 'stale')
        repaired = m.execute_plan(obsolete, self.journal, authorized=True, authorize_enrichment=True, enrichment=repair,
                                 now=now, transport_binding=TRANSPORT)
        self.assertEqual(repaired['status'], 'completed', repaired)
        self.assertEqual(repair.client.session.calls, 2)
        self.assertEqual(len(list((self.journal/'enrichment-attempts').glob('*.json'))), 2)

    def test_interrupted_materiality_reservation_blocks_new_plan(self):
        self.seed()
        state = m.build_plan(self.path, COHORT, now=T0)
        repair = offline_enrichment(state['sources'][1]['jobs'][0]['canonical_id'], self.journal)
        plan = m.build_plan(self.path, COHORT, now=T0, phase='derived', enrichment=repair, transport_binding=TRANSPORT)
        repair.client.session.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            m.execute_plan(plan,self.journal,authorized=True,authorize_enrichment=True,enrichment=repair,
                           now=T0,transport_binding=TRANSPORT)
        self.assertEqual(m.report(self.journal,plan['plan_id'])['status'],'interrupted')
        fresh=m.build_plan(self.path,COHORT,now=T0+timedelta(seconds=1),phase='derived',enrichment=repair)
        self.assertIn('enrichment_attempt_already_consumed',fresh['enrichment_scope']['blocked'])
        self.assertEqual(repair.client.session.calls,1)
        other = offline_enrichment(repair.canonical_id, self.directory/'different-journal')
        changed = m.build_plan(self.path, COHORT, now=T0, phase='derived', enrichment=other,
                               transport_binding=TRANSPORT)
        self.assertIn('maintenance_journal_root_changed', changed['enrichment_scope']['blocked'])
        with self.assertRaisesRegex(ValueError, 'journal_root_changed'):
            m.execute_plan(changed, other.journal_root, authorized=True, authorize_enrichment=True,
                           enrichment=other, now=T0, transport_binding=TRANSPORT)
        self.assertEqual(other.client.session.calls, 0)

    def test_partial_model_response_keeps_raw_prefix_and_consumed_attempt(self):
        self.seed(); seed_owner(self.path, self.directory)
        owner = owner_for(self.directory, T0)
        from tests.professional_background_preparation_support import OfflineResponse
        class InterruptedResponse(OfflineResponse):
            def iter_content(self, chunk_size):
                yield b'OFFLINE incomplete model prefix'
                raise KeyboardInterrupt('synthetic interrupted stream')
        owner.preparer._client.session.post = lambda *a, **k: InterruptedResponse(b'')
        plan = m.build_plan(self.path, COHORT, now=T0, owner=owner, phase='derived', transport_binding=TRANSPORT)
        with self.assertRaises(KeyboardInterrupt):
            m.execute_plan(plan, self.journal, authorized=True, authorize_preparation=True,
                           owner=owner, now=T0, transport_binding=TRANSPORT)
        receipt = m.report(self.journal, plan['plan_id'])
        event = next(e for e in receipt['events'] if e['event'] == 'preparation_audit'
                     and e['data']['event'] == 'partial_response')
        self.assertFalse(event['data']['capture_complete'])
        self.assertEqual((self.journal/plan['plan_id']/event['data']['raw_response']['file']).read_bytes(),
                         b'OFFLINE incomplete model prefix')
        again = m.build_plan(self.path, COHORT, now=T0, owner=owner_for(self.directory, T0), phase='derived')
        self.assertEqual(again['owner_scope']['items'][0]['reason'], 'professional_evidence_attempted')

    def test_detail_incomplete_read_preserves_unaccepted_partial_response(self):
        from http.client import IncompleteRead
        self.seed()
        accepted = m.build_plan(self.path, ['alignerr'], now=T0)['sources'][0]['jobs'][0]['evidence']['accepted_capture_id']
        from tests.evidence_maintenance_support import BytesResponse
        original = BytesResponse.read
        def interrupted(response, *args):
            raw = original(response, *args)
            if b'<html' in raw.lower() or b'<!doctype' in raw.lower():
                raise IncompleteRead(b'OFFLINE partial detail', 100)
            return raw
        now = T0 + timedelta(hours=73)
        with patch.object(BytesResponse, 'read', interrupted):
            plan, receipt = source_execute(self.path, self.journal, now, changed=True, held_detail=True)
        partial = [e for e in receipt['events'] if e['event'] == 'source_transport'
                   and e['data'].get('capture_complete') is False]
        self.assertTrue(partial, receipt)
        after = m.build_plan(self.path, ['alignerr'], now=now)['sources'][0]['jobs'][0]
        self.assertEqual(after['evidence']['accepted_capture_id'], accepted)

    def test_oversized_detail_prefix_is_incomplete_and_keeps_http_status(self):
        from tests.test_provider_detail_recovery import CASES, candidate
        from tests.evidence_maintenance_support import BytesResponse
        from wahojobs.crawler.provider_details import fetch_detail, MAX_DETAIL_BYTES
        from wahojobs.crawler.local_inventory import refresh_request_budget
        selected = candidate(CASES[0])
        events = []
        with refresh_request_budget(audit_sink=events.append), patch('wahojobs.crawler.provider_details.build_opener') as opener:
            opener.return_value.open.return_value = BytesResponse(b'x'*(MAX_DETAIL_BYTES+1), selected.url)
            with self.assertRaisesRegex(ValueError, 'size limit'):
                fetch_detail('alignerr', selected)
        raw = next(e for e in events if 'raw_response' in e)
        self.assertFalse(raw['capture_complete'])
        self.assertEqual(len(raw['raw_response']), MAX_DETAIL_BYTES+1)
        self.assertEqual(events[-1]['status'], 200)

    def test_oversized_model_prefix_is_bounded_and_unaccepted(self):
        from tests.professional_background_preparation_support import OfflineClient, OfflineResponse
        from wahojobs.opportunity_llm import OpenAIEnrichmentError
        client = OfflineClient()
        client.session.post = lambda *a, **k: OfflineResponse(b'x'*100)
        complete, partial = [], []
        with self.assertRaises(OpenAIEnrichmentError):
            client.generate_structured({}, prompt='offline', schema={}, schema_name='offline',
                max_output_tokens=1, max_response_bytes=10, response_sink=complete.append,
                partial_response_sink=partial.append)
        self.assertEqual(complete, [])
        self.assertEqual(partial, [b'x'*11])

    def test_held_detail_and_hard_http_cap_preserve_strong_binding(self):
        self.seed()
        before=m.build_plan(self.path,COHORT,now=T0)['sources'][0]['jobs'][0]['evidence']['accepted_capture_id']
        now=T0+timedelta(hours=73)
        _, held=source_execute(self.path,self.journal,now,held_detail=True,changed=True)
        after=m.build_plan(self.path,COHORT,now=now)['sources'][0]['jobs'][0]
        self.assertEqual(after['evidence']['accepted_capture_id'],before)
        results=[e['data'] for e in held['events'] if e['event']=='operation_result' and e['data']['operation']=='catalog:alignerr']
        self.assertEqual(results[0]['result']['detail_counts']['failed'],1)
        later=now+timedelta(hours=73)
        plan=m.build_plan(self.path,['alignerr'],now=later,http_limit=1,detail_limit=0,transport_binding=TRANSPORT)
        with offline_transport(later,partial=True):
            result=m.execute_plan(plan,self.journal,authorized=True,authorize_sources=True,now=later,transport_binding=TRANSPORT)
        usage=result['events'][-1]['data']['request_usage']
        self.assertEqual(usage['http_transactions'],1)
        self.assertEqual(result['status'],'partially_completed')

    def test_endpoint_and_owner_budget_drift_are_rejected(self):
        plan=source_plan(self.path,T0)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE companies SET careers_url='https://unexpected.test/catalog' WHERE slug='mercor'")
        with self.assertRaisesRegex(ValueError,'source_changed'):
            m.execute_plan(plan,self.journal,authorized=True,authorize_sources=True,now=T0,transport_binding=TRANSPORT)
        # Restore only synthetic configuration through a new fixture operation.
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE companies SET careers_url='https://aws.api.mercor.com/work/listings-explore-page' WHERE slug='mercor'")
        self.seed();seed_owner(self.path,self.directory)
        owner=owner_for(self.directory,T0)
        plan=m.build_plan(self.path,COHORT,now=T0,owner=owner,phase='derived',transport_binding=TRANSPORT)
        owner.preparer._budget=None
        with self.assertRaisesRegex(ValueError,'owner_or_configuration_changed'):
            m.execute_plan(plan,self.journal,authorized=True,authorize_preparation=True,owner=owner,now=T0,transport_binding=TRANSPORT)

    def test_interrupted_durable_preparation_keeps_attempt_consumed(self):
        self.seed();seed_owner(self.path,self.directory)
        owner=owner_for(self.directory,T0)
        plan=m.build_plan(self.path,COHORT,now=T0,owner=owner,phase='derived',transport_binding=TRANSPORT)
        owner.preparer._client.session.after_request=lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            m.execute_plan(plan,self.journal,authorized=True,authorize_preparation=True,owner=owner,now=T0,transport_binding=TRANSPORT)
        restarted=owner_for(self.directory,T0)
        pending=m.build_plan(self.path,COHORT,now=T0,owner=restarted,phase='derived')
        self.assertEqual(pending['owner_scope']['items'][0]['reason'],'professional_evidence_attempted')
        self.assertEqual(len(restarted.preparer._client.session.calls),0)

    def test_raw_http_error_and_tampered_journal_are_not_accepted(self):
        from urllib.error import HTTPError
        from email.message import Message
        import io
        from wahojobs.crawler.local_inventory import open_catalog, refresh_request_budget
        from urllib.request import Request
        events=[]
        failure=HTTPError('https://aws.api.mercor.com/work/listings-explore-page',429,'offline',Message(),io.BytesIO(b'OFFLINE limited'))
        with refresh_request_budget(audit_sink=events.append), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=failure
            with self.assertRaises(HTTPError):
                open_catalog(Request(failure.url),timeout=30)
        self.assertTrue(any(e.get('raw_response')==b'OFFLINE limited' for e in events))
        plan,result=self.seed()
        raw=next((self.journal/plan['plan_id']).glob('*.raw'))
        raw.write_bytes(b'synthetic corruption')
        with self.assertRaisesRegex(ValueError,'response_integrity'):
            m.report(self.journal,plan['plan_id'])

    def test_pagination_transport_failure_never_accepts_partial_pages_as_complete(self):
        self.seed()
        now=T0+timedelta(hours=73)
        _, result=source_execute(self.path,self.journal,now,pagination_failure=True)
        state=m.build_plan(self.path,COHORT,now=now)
        self.assertEqual(state['sources'][0]['latest_run']['status'],'failed')
        self.assertEqual(state['sources'][0]['jobs'][0]['verification']['status'],'stale_source')
        self.assertEqual(result['status'],'partially_completed')

    def test_edited_rehashed_operations_cannot_widen_source_or_canonical_scope(self):
        from copy import deepcopy
        plan=source_plan(self.path,T0)
        edited=deepcopy(plan)
        edited['operations'][0]['provider']='appen'
        edited['plan_id']=m.digest({k:v for k,v in edited.items() if k!='plan_id'})
        with self.assertRaisesRegex(ValueError,'operations_changed'):
            m.execute_plan(edited,self.journal,authorized=True,authorize_sources=True,now=T0,transport_binding=TRANSPORT)
        self.assertFalse(self.journal.exists())

    def test_scoped_inspection_does_not_write_main_or_companion(self):
        self.seed();seed_owner(self.path,self.directory)
        owner=owner_for(self.directory,T0,enabled=False)
        owner.preparer._budget=None
        before=(self.path.read_bytes(),(self.directory/'professional-background.sqlite3').read_bytes())
        plan=m.build_plan(self.path,COHORT,now=T0,owner=owner,phase='derived')
        self.assertEqual(before,(self.path.read_bytes(),(self.directory/'professional-background.sqlite3').read_bytes()))
        item=plan['owner_scope']['items'][0]
        self.assertEqual(item['state'],'needs_preparation')
        self.assertIn('preparation_budget_absent',plan['operations'][-1]['blocked'])
        self.assertNotIn('source_inputs_unavailable',plan['operations'][-1]['blocked'])

    def test_zero_token_budget_and_source_only_authority_do_not_dispatch_model(self):
        from wahojobs.professional_background_preparation import PreparationBudget
        self.seed();seed_owner(self.path,self.directory)
        owner=owner_for(self.directory,T0)
        owner.preparer._budget=PreparationBudget(1,1)
        plan=m.build_plan(self.path,COHORT,now=T0,owner=owner,phase='derived',transport_binding=TRANSPORT)
        result=m.execute_plan(plan,self.journal,authorized=True,authorize_preparation=True,owner=owner,now=T0,transport_binding=TRANSPORT)
        self.assertEqual(result['status'],'awaiting_authorized_repair')
        self.assertEqual(len(owner.preparer._client.session.calls),0)

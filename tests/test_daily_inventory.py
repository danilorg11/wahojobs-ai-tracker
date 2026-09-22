"""Isolated daily operations: real maintenance/backup, retained fixtures, fake services."""
from contextlib import closing
from datetime import timedelta
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch,Mock

from wahojobs import daily_inventory as d,evidence_maintenance as m
from wahojobs.maintenance_gate import operation_gate
from scripts import daily_inventory as cli
from tests.evidence_maintenance_support import new_inventory,source_execute,offline_transport,T0,protected_state


class DailyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='daily-inventory-tests-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.at=T0.replace(hour=6)
        self.config=dict(state_directory=str(self.root),database=str(self.root/'inventory.sqlite3'),first_run_at=d.stamp(self.at))
        with operation_gate(self.config['database']):pass

    def test_daily_slot_restart_missed_window_and_next_execution(self):
        self.assertEqual(d.next_trigger(self.at-timedelta(seconds=1)),self.at)
        self.assertEqual(d.next_trigger(self.at),self.at+timedelta(days=1))
        self.assertIsNone(d.reserve_run(self.root,self.at-timedelta(seconds=1),self.at,'timer'))
        first=d.reserve_run(self.root,self.at+timedelta(minutes=10),self.at,'restart')
        self.assertEqual(first['outcome'],'reserved')
        self.assertIsNone(d.reserve_run(self.root,self.at+timedelta(minutes=11),self.at,'manual'))
        late=d.reserve_run(self.root,self.at+timedelta(days=3,hours=2),self.at,'restart')
        self.assertEqual(late['outcome'],'missed_window')
        self.assertEqual(len(list((self.root/'runs').iterdir())),2)
        self.assertIsNone(d.reserve_run(self.root,self.at+timedelta(days=3,hours=2),self.at,'timer'))

    def test_policy_disabled_preview_budgets_and_missing_delivery_fail_closed(self):
        policy=json.loads((Path(__file__).parents[1]/'deploy/private-beta/daily-inventory-v1.example.json').read_text(encoding='utf-8-sig'))
        policy['code_commit']='a'*40
        d.validate_policy(policy)
        with self.assertRaises(ValueError):d.validate_policy(policy,activation=True)
        policy['enabled']=True
        with self.assertRaises(ValueError):d.validate_policy(policy,activation=True)
        for field,value in [('database','/tmp/preview.sqlite3'),('sources',{'alignerr':101,'mercor':1}),('model_calls',1),('execution_seconds',901),('runtime_config','/tmp/synthetic.json')]:
            with self.subTest(field=field),self.assertRaises(ValueError):d.validate_policy(dict(policy,**{field:value}))

    def test_gate_excludes_another_process_and_releases_without_deleting_inode(self):
        path=self.root/'inventory.sqlite3'
        code="from wahojobs.maintenance_gate import operation_gate;from pathlib import Path;import sys\nwith operation_gate(Path(sys.argv[1])):pass"
        with operation_gate(path):
            child=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,timeout=10)
            self.assertNotEqual(child.returncode,0)
        child=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,timeout=10)
        self.assertEqual(child.returncode,0,child.stderr)
        self.assertTrue(Path(str(path)+'.wahojobs-maintenance.lock').exists())

    def test_supervisor_restores_on_worker_failure_timeout_and_missing_receipt(self):
        for error in (RuntimeError('process failed'),subprocess.TimeoutExpired('worker',900),None):
            root=self.root/str(len(list(self.root.iterdir())))
            config=dict(self.config,state_directory=str(root))
            operations=Mock()
            operations.collect.side_effect=error
            with patch.object(d,'now',return_value=self.at):
                result=cli.supervise(config,self.root/'policy','timer',operations=operations)
            self.assertEqual(result['outcome'],'failed')
            self.assertTrue(result['normal_service_resumed'])
            operations.restore.assert_called_once_with(120)
            with patch.object(d,'now',return_value=self.at):
                second=cli.supervise(config,self.root/'policy','timer',operations=operations)
            self.assertEqual(second['outcome'],'already_consumed_or_not_due')
            self.assertEqual(operations.collect.call_count,1)

    def test_failed_stop_still_restores_and_failed_restore_is_visible(self):
        operations=Mock();operations.stop.side_effect=TimeoutError();operations.restore.side_effect=OSError()
        with patch.object(d,'now',return_value=self.at):result=cli.supervise(self.config,self.root/'policy','timer',operations=operations)
        self.assertEqual(result['outcome'],'recovery_failed');self.assertFalse(result['normal_service_resumed'])
        operations.collect.assert_not_called()

    def test_supervisor_receipt_success_requires_source_qualification(self):
        operations=Mock()
        def collect(run_id,remaining):
            target=self.root/'runs'/run_id
            d.write_json(target/'worker.json',dict(completed=True,protected_domains_unchanged=True))
            for source in d.SOURCES:d.write_json(target/(source+'.json'),dict(qualifying_observation=source=='mercor'))
        operations.collect.side_effect=collect
        with patch.object(d,'now',return_value=self.at):result=cli.supervise(self.config,self.root/'policy','timer',operations=operations)
        self.assertEqual(result['outcome'],'partial_or_failed')
        self.assertTrue(result['normal_service_resumed'])

    def test_process_timeout_kills_worker_group_before_return(self):
        proc=Mock(pid=123);proc.wait.side_effect=[subprocess.TimeoutExpired('worker',1),-9]
        with patch.object(cli.subprocess,'Popen',return_value=proc),patch.object(cli.os,'killpg',create=True) as kill,patch.object(cli.signal,'SIGKILL',9,create=True):
            with self.assertRaises(subprocess.TimeoutExpired):cli.bounded_process(['fixture'],timeout=1)
        kill.assert_called_once_with(123,signal_value())
        self.assertEqual(proc.wait.call_count,2)

    def test_hourly_alert_threshold_cohorts_missed_run_dedup_and_recovery(self):
        source=dict(outcome='complete',qualifying_observation=True,observed=100,cohorts=[
            dict(verified_at=d.stamp(self.at),records=90),dict(verified_at=d.stamp(self.at-timedelta(hours=35)),records=10)])
        for name in d.SOURCES:d.write_json(self.root/(name+'-state.json'),source)
        initial=d.health(self.config,self.at)
        self.assertEqual(set(initial['active']),{s+':coverage' for s in d.SOURCES if d.POLICY[s]['readiness']=='blocked'})
        warning=d.health(self.config,self.at+timedelta(hours=1))
        self.assertEqual(warning['active']['mercor:age36']['records'],10)
        self.assertIn('run:missing',warning['active'])
        repeated=d.health(self.config,self.at+timedelta(hours=2))
        self.assertEqual(len(warning['events']),len(repeated['events']))
        escalation=d.health(self.config,self.at+timedelta(hours=13))
        self.assertIn('mercor:age48',escalation['active'])
        self.assertEqual(escalation['active']['mercor:age48']['records'],10)
        for name in d.SOURCES:
            refreshed=dict(source,cohorts=[dict(verified_at=d.stamp(self.at+timedelta(hours=13)),records=100)])
            d.write_json(self.root/(name+'-state.json'),refreshed)
        recovered=d.health(self.config,self.at+timedelta(hours=14))
        self.assertTrue(any(e['kind']=='recovered' and e['key']=='mercor:age48' for e in recovered['events']))
        self.assertNotIn('mercor:age48',recovered['active'])

    def test_alert_delivery_is_single_attempt_and_ambiguous_failure_not_retried(self):
        config=dict(self.config,alert_delivery=dict(command=['/reviewed/adapter'],recipient='configured-fixture'))
        state=dict(events=[dict(id='fixture',delivery='pending')])
        with patch.object(cli.subprocess,'run',side_effect=subprocess.TimeoutExpired('delivery',15)) as transport:
            cli.deliver(config,state);cli.deliver(config,state)
        self.assertEqual(transport.call_count,1)
        self.assertEqual(state['events'][0]['delivery'],'failed_or_uncertain')


    def test_receipt_write_failure_cannot_prevent_service_restoration(self):
        operations=Mock();operations.collect.side_effect=TimeoutError()
        original=d.write_json;calls=[]
        def failing(path,value):
            calls.append(path)
            if len(calls)>2:raise OSError('fixture disk full')
            return original(path,value)
        with patch.object(d,'now',return_value=self.at),patch.object(d,'write_json',side_effect=failing):
            result=cli.supervise(self.config,self.root/'policy','timer',operations=operations)
        operations.restore.assert_called_once_with(120)
        self.assertTrue(result['normal_service_resumed'])

    def test_recovery_remaining_allowance_and_persistence_failure(self):
        receipt=d.reserve_run(self.root,self.at,self.at,'timer')
        receipt.update(maintenance_started_at=d.stamp(self.at),recovery_started_at=d.stamp(self.at+timedelta(seconds=20)),normal_service_resumed=False,outcome='failed')
        target=self.root/'runs'/receipt['run_id']/'run.json';d.write_json(target,receipt)
        with patch.object(d,'now',return_value=self.at+timedelta(seconds=50)),patch.object(cli,'NativeOperations') as native,patch.object(d,'write_json',side_effect=OSError('fixture disk full')):
            with self.assertRaises(OSError):cli.recover(self.config,self.root/'policy')
        native.return_value.restore.assert_called_once_with(90)

    def test_worker_dispatch_is_bound_to_one_current_parent_and_deadline(self):
        receipt=d.reserve_run(self.root,self.at,self.at,'timer')
        target=self.root/'runs'/receipt['run_id']/'run.json'
        with self.assertRaises(ValueError):cli.claim_dispatch(self.root,receipt['run_id'],123,50)
        receipt.update(outcome='running',supervisor_pid=123,execution_deadline_monotonic=900)
        d.write_json(target,receipt)
        with self.assertRaises(ValueError):cli.claim_dispatch(self.root,receipt['run_id'],124,50)
        with self.assertRaises(ValueError):cli.claim_dispatch(self.root,receipt['run_id'],123,901)
        self.assertEqual(cli.claim_dispatch(self.root,receipt['run_id'],123,50),850)
        with self.assertRaises(FileExistsError):cli.claim_dispatch(self.root,receipt['run_id'],123,51)

    def test_count_drop_requires_real_recovery_and_failure_keeps_cohort_ages(self):
        previous=dict(observed=1000,cohorts=[{'verified_at':d.stamp(self.at),'records':1000}],last_qualifying_verification=d.stamp(self.at),next_verification_deadline=d.stamp(self.at+timedelta(hours=72)))
        def observation(n):return dict(observed=n,cohorts=[],last_qualifying_verification=None,next_verification_deadline=None)
        low=d.merge_source_history(observation(200),previous)
        self.assertTrue(low['abnormal_count_drop']);self.assertEqual(low['count_baseline'],1000)
        still_low=d.merge_source_history(observation(200),low)
        self.assertTrue(still_low['abnormal_count_drop']);self.assertEqual(still_low['cohorts'],previous['cohorts'])
        recovered=d.merge_source_history(observation(900),still_low)
        self.assertFalse(recovered['abnormal_count_drop'])
        config=dict(self.config,first_run_at=d.stamp(self.at+timedelta(days=3)))
        self.assertEqual(d.health(config,self.at)['next_scheduled_execution'],config['first_run_at'])

    def test_native_trigger_uses_actual_timer_record(self):
        for fired,expected in [(self.at,'timer'),(self.at+timedelta(minutes=10),'timer_catch_up')]:
            environment=dict(TRIGGER_UNIT='wahojobs-inventory.timer',TRIGGER_TIMER_REALTIME_USEC=str(int(fired.timestamp()*1000000)),INVOCATION_ID='a'*32)
            with patch.dict(os.environ,environment,clear=True),patch.object(d,'now',return_value=fired+timedelta(seconds=120)):
                result=cli.native_trigger()
                self.assertEqual(result['trigger'],expected);self.assertEqual(result['triggered_at'],d.stamp(fired))
                self.assertEqual(result['invocation_id'],'a'*32)
        with patch.dict(os.environ,{},clear=True):self.assertEqual(cli.native_trigger()['trigger'],'unknown')

    def test_native_boot_order_includes_implicit_timer_and_service_dependencies(self):
        from configparser import ConfigParser
        from graphlib import TopologicalSorter
        root=Path(__file__).parents[1]/'deploy/private-beta'
        graph={'basic.target':{'timers.target'},'network-online.target':{'basic.target'}}
        for path in root.glob('wahojobs-*.service'):
            graph.setdefault(path.name,set()).add('basic.target')
        for path in list(root.glob('wahojobs-inventory*.service'))+list(root.glob('wahojobs-inventory*.timer')):
            config=ConfigParser(interpolation=None);config.read(path,encoding='utf-8')
            for name in config['Unit'].get('After','').split():graph.setdefault(path.name,set()).add(name)
            for name in config['Unit'].get('Before','').split():graph.setdefault(name,set()).add(path.name)
            if path.suffix=='.timer':
                graph.setdefault('timers.target',set()).add(path.name)
                graph.setdefault(config['Timer']['Unit'],set()).add(path.name)
        order=list(TopologicalSorter(graph).static_order())
        self.assertLess(order.index('wahojobs-inventory-recovery.service'),order.index('wahojobs-inventory.service'))

    def test_new_day_grace_is_not_false_recovery_from_a_missing_run(self):
        initial=d.health(self.config,self.at+timedelta(hours=1))
        self.assertIn('run:missing',initial['active'])
        during=d.health(self.config,self.at+timedelta(days=1,minutes=2))
        self.assertIn('run:missing',during['active'])
        self.assertFalse(any(e['key']=='run:missing' and e['kind']=='recovered' for e in during['events']))
        next_day=self.at+timedelta(days=1)
        receipt=d.reserve_run(self.root,next_day,next_day,'timer');receipt['outcome']='partial_individual'
        d.write_json(self.root/'runs'/receipt['run_id']/'run.json',receipt)
        recovered=d.health(self.config,next_day+timedelta(minutes=3))
        self.assertNotIn('run:missing',recovered['active'])

    def test_late_boot_recovery_only_observes_already_normal_service(self):
        receipt=d.reserve_run(self.root,self.at,self.at,'timer')
        receipt.update(maintenance_started_at=d.stamp(self.at),normal_service_resumed=False,outcome='running')
        target=self.root/'runs'/receipt['run_id']/'run.json';d.write_json(target,receipt)
        with patch.object(d,'now',return_value=self.at+timedelta(hours=2)),patch.object(cli,'NativeOperations') as native:
            cli.recover(self.config,self.root/'policy')
        native.return_value.recovery_preflight.assert_called_once()
        native.return_value.restore.assert_not_called();native.return_value.ready.assert_called_once()
        self.assertTrue(d.read_json(target)['normal_service_resumed'])
        self.assertIsNone(d.read_json(target)['maintenance_seconds'])

    def test_failed_source_keeps_aging_cohorts_and_unknown_counts(self):
        old=d.stamp(self.at-timedelta(hours=73))
        previous=dict(observed=100,count_baseline=100,cohorts=[dict(verified_at=old,records=40)],last_qualifying_verification=old)
        summary=d.summarize_source(dict(plan_id='fixture',config=dict(providers=['mercor']),sources=[dict(jobs=[])]),{},self.at,self.at)
        merged=d.merge_source_history(summary,previous)
        self.assertIsNone(merged['observed']);self.assertIsNone(merged['missing']);self.assertEqual(merged['stale'],40)
        self.assertEqual(merged['count_baseline'],100)

    def test_interrupted_source_reporting_counts_durable_attempts_without_collection(self):
        receipt=d.reserve_run(self.root,self.at,self.at,'timer');receipt['maintenance_seconds']=12
        plan=dict(plan_id='a'*64,config=dict(providers=['alignerr']),sources=[dict(jobs=[])])
        d.write_json(self.root/'runs'/receipt['run_id']/'alignerr-plan.json',plan)
        journal=dict(status='interrupted',events=[dict(event='source_transport',data=dict(event='request'))]*7)
        with patch.object(m,'report',return_value=journal),patch.object(d,'now',return_value=self.at):
            d.finish_run_sources(dict(self.config,journal=str(self.root/'journal')),receipt)
        self.assertEqual(receipt['sources']['alignerr']['requests_used'],7)
        self.assertFalse(receipt['sources']['alignerr']['qualifying_observation'])
        self.assertEqual(receipt['sources']['mercor']['outcome'],'not_started')
        self.assertEqual(receipt['sources']['mercor']['requests_used'],0)
        self.assertEqual(receipt['sources']['alignerr']['maintenance_seconds'],12)

    def test_old_recovery_cannot_replace_newer_qualifying_provider_state(self):
        receipt=d.reserve_run(self.root,self.at,self.at,'timer')
        newer_id=(self.at+timedelta(days=1)).strftime('%Y%m%dT060000Z')
        newest=dict(run_id=newer_id,qualifying_observation=True,outcome='complete',observed=100,
            cohorts=[dict(verified_at=d.stamp(self.at+timedelta(days=1)),records=100)])
        for provider in d.SOURCES:d.write_json(self.root/(provider+'-state.json'),newest)
        with patch.object(d,'now',return_value=self.at+timedelta(days=1)):
            d.finish_run_sources(self.config,receipt)
        for provider in d.SOURCES:
            self.assertEqual(d.read_json(self.root/(provider+'-state.json')),newest)
            old=d.read_json(self.root/'runs'/receipt['run_id']/(provider+'.json'))
            self.assertEqual(old['outcome'],'blocked' if d.POLICY[provider]['readiness']=='blocked' else 'not_started');self.assertEqual(old['cohorts'],[])


    def test_deadline_prevents_new_physical_request_and_redirect_does_not_expand_budget(self):
        from urllib.request import Request,build_opener
        from urllib.error import HTTPError
        from wahojobs.crawler.local_inventory import request_deadline,refresh_request_budget,open_catalog
        endpoint='https://aws.api.mercor.com/work/listings-explore-page'
        with patch('wahojobs.crawler.local_inventory.time.monotonic',return_value=10),request_deadline(9),patch('urllib.request.build_opener') as opener,refresh_request_budget(http_limit=1,detail_limit=0) as budget:
            with self.assertRaises(TimeoutError):open_catalog(Request(endpoint),timeout=30)
            self.assertEqual(budget.summary()['http_transactions'],0);opener.assert_not_called()
        from wahojobs.crawler.provider_details import _NoRedirect
        with self.assertRaises(HTTPError):build_opener(_NoRedirect()).error('http',Request(endpoint),None,302,'Found',{'location':'https://other.example/'})
        with patch('urllib.request.build_opener') as opener,refresh_request_budget(http_limit=1,detail_limit=0) as budget:
            opener.return_value.open.side_effect=OSError('fixture redirect refused')
            with self.assertRaises(OSError):open_catalog(Request(endpoint),timeout=30)
            with self.assertRaises(OSError):open_catalog(Request(endpoint),timeout=30)
            self.assertEqual(opener.return_value.open.call_count,1)
            self.assertEqual(budget.summary()['http_transactions'],1)


def signal_value():
    import signal
    return getattr(signal,'SIGKILL',9)


class DailyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='daily-real-pipeline-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.database=new_inventory(self.root/'fixture');self.journal=self.root/'fixture'/'journal'
        source_execute(self.database,self.journal,T0)

    def test_due_inside_freshness_real_worker_backup_and_user_data_preserved(self):
        fresh=T0+timedelta(hours=24)
        ordinary=m.build_plan(self.database,['alignerr'],now=fresh,http_limit=100,details=None,phase='source')
        self.assertFalse(any(o['kind']=='catalog_observation' for o in ordinary['operations']))
        daily=m.build_plan(self.database,['alignerr'],now=fresh,http_limit=100,details=None,phase='source',daily_discovery=True)
        self.assertTrue(any(o['kind']=='catalog_observation' for o in daily['operations']))
        before=d.protected_domains(self.database)
        config=dict(database=str(self.database),journal=str(self.journal),state_directory=str(self.root/'state'),code_commit='a'*40)
        config['sources']=d.default_sources()
        for source,row in config['sources'].items():row['enabled']=source in ('alignerr','mercor')
        with offline_transport(fresh) as calls,patch.object(d,'now',return_value=fresh):d.collect(config,'fixture')
        self.assertEqual(len(calls),2)
        self.assertEqual(before,d.protected_domains(self.database))
        state=d.read_json(self.root/'state'/'mercor-state.json')
        self.assertTrue(state['qualifying_observation']);self.assertEqual(state['missing'],1)
        self.assertEqual(state['confirmed_closed'],0);self.assertEqual(state['changed'],0)
        self.assertEqual(len(state['cohorts']),2)
        with m.read_connection(self.database) as db:
            missing=db.execute("SELECT is_active,last_seen_at FROM jobs WHERE external_id='synthetic-maintenance-absent'").fetchone()
            self.assertEqual(missing['is_active'],1);self.assertEqual(missing['last_seen_at'],T0.isoformat())
        self.assertTrue(d.read_json(self.root/'state'/'runs'/'fixture'/'backup.json')['verified'])
        self.assertTrue(d.read_json(self.root/'state'/'runs'/'fixture'/'worker.json')['protected_domains_unchanged'])

    def test_partial_capped_alignerr_cannot_close_or_qualify(self):
        fresh=T0+timedelta(hours=24)
        plan=m.build_plan(self.database,['alignerr'],now=fresh,http_limit=1,details=None,phase='source',daily_discovery=True)
        with offline_transport(fresh,partial=True) as calls:
            result=m.execute_plan(plan,self.journal,authorized=True,authorize_sources=True,now=fresh)
        summary=d.summarize_source(plan,result,fresh,fresh)
        self.assertEqual(len(calls),1);self.assertEqual(summary['requests_used'],1)
        self.assertFalse(summary['qualifying_observation']);self.assertEqual(summary['confirmed_closed'],0)
        with m.read_connection(self.database) as db:self.assertEqual(db.execute("SELECT count(*) FROM jobs j JOIN companies c ON c.id=j.company_id WHERE c.slug='alignerr' AND is_active=1").fetchone()[0],1)

    def test_failed_mercor_is_not_no_exception_success(self):
        fresh=T0+timedelta(hours=24)
        plan=m.build_plan(self.database,['mercor'],now=fresh,http_limit=1,details=None,phase='source',daily_discovery=True)
        with offline_transport(fresh,fail_mercor=True) as calls:
            result=m.execute_plan(plan,self.journal,authorized=True,authorize_sources=True,now=fresh)
        summary=d.summarize_source(plan,result,fresh,fresh)
        self.assertEqual(summary['requests_used'],1);self.assertFalse(summary['qualifying_observation'])
        self.assertEqual(summary['outcome'],'failed')

    def test_manual_maintenance_is_excluded_by_supervisor_gate(self):
        plan=m.build_plan(self.database,['mercor'],now=T0,http_limit=1,details=None,phase='source',daily_discovery=True)
        with operation_gate(self.database),self.assertRaises(OSError):
            m.execute_plan(plan,self.journal,authorized=True,authorize_sources=True,now=T0)


    def test_hard_100_alignerr_requests_still_leave_101_page_snapshot_incomplete(self):
        from tests.evidence_maintenance_support import BytesResponse,listing,CASES
        from urllib.parse import urlsplit,parse_qs
        fresh=T0+timedelta(hours=24)
        plan=m.build_plan(self.database,['alignerr'],now=fresh,http_limit=100,details=None,phase='source',daily_discovery=True)
        calls=[]
        class Pages:
            def open(self,request,timeout):
                calls.append(request.full_url)
                offset=int(parse_qs(urlsplit(request.full_url).query)['offset'][0])
                record=dict(listing(CASES[0]),id='fixture_'+str(offset),applyUrl='https://www.alignerr.com/jobs/fixture_'+str(offset))
                return BytesResponse(json.dumps(dict(jobs=[record],limit=1,offset=offset,total=101)).encode(),request.full_url)
        with patch('urllib.request.build_opener',return_value=Pages()),patch('wahojobs.crawler.pipeline.utc_now',return_value=fresh.isoformat()):
            result=m.execute_plan(plan,self.journal,authorized=True,authorize_sources=True,now=fresh)
        summary=d.summarize_source(plan,result,fresh,fresh)
        self.assertEqual(len(calls),100);self.assertEqual(summary['requests_used'],100)
        self.assertFalse(summary['qualifying_observation']);self.assertEqual(summary['confirmed_closed'],0)

if __name__=='__main__':unittest.main()

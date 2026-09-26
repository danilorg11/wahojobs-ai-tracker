"""Isolated operational receipt/notification cases; no provider or mail calls."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wahojobs import daily_inventory as daily, operational_email as email
from wahojobs import evidence_maintenance as maintenance
from tests import test_daily_source_coverage as coverage
from tests.evidence_maintenance_support import T0
from datetime import timedelta
from contextlib import closing
import sqlite3
import json
import subprocess
import sys


class ReceiptClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.at=daily.parse('2026-09-26T10:40:00Z')
        self.config=dict(state_directory=str(self.root),database=str(self.root/'unused.db'),
            journal=str(self.root/'journal'),first_run_at='2026-09-23T06:00:00Z',sources=daily.default_sources())
        self.config['sources']['micro1']['enabled']=False
        self.run_id='20260926T060000Z';self.target=self.root/'runs'/self.run_id
        self.target.mkdir(parents=True)

    def row(self,provider,outcome,requests=0,**extra):
        row=daily.empty_source(provider,self.at,outcome=outcome)
        row.update(run_id=self.run_id,requests_used=requests,**extra)
        return row

    def install(self):
        qualified={'appen','outlier','rws'}
        failures={'dataannotation','dataforce','mindrift','oneforma','turing','welocalize'}
        rows={}
        for provider in daily.SOURCES:
            if provider in qualified:row=self.row(provider,'complete',1,qualifying_observation=True)
            elif provider in failures:row=self.row(provider,'failed',1)
            elif provider=='micro1':row=self.row(provider,'disabled')
            else:row=self.row(provider,'accounting_unavailable',None)
            daily.write_json(self.root/(provider+'-state.json'),row);rows[provider]=row
        receipt=dict(run_id=self.run_id,outcome='partial_or_failed',normal_service_resumed=True,
            scheduled_at='2026-09-26T06:00:00Z',ended_at='2026-09-26T06:06:17Z',sources=rows)
        daily.write_json(self.target/'run.json',receipt)
        daily.write_json(self.target/'worker.json',dict(completed=True,protected_domains_unchanged=True))
        return rows

    def test_six_failures_five_unknown_remain_distinct_in_totals_and_email(self):
        self.install()
        with patch.object(daily,'_baseline_cohorts',return_value={}):state=daily.health(self.config,self.at)
        cycle=state['context']['cycle']
        self.assertEqual(cycle['state'],'partial')
        self.assertEqual(len(cycle['qualified_sources']),3)
        self.assertEqual(len(cycle['failed_sources']),6)
        self.assertEqual(cycle['accounting_unavailable_sources'],['alignerr','handshake','mercor','meridial','surge'])
        self.assertIsNone(cycle['requests_used'])
        rendered=email.message([e for e in state['events'] if e['delivery']=='pending'],state['context'])['text']
        self.assertIn('6 attempted sources failed',rendered)
        self.assertIn('5 source accounting unavailable',rendered)
        self.assertIn('Alignerr: source accounting unavailable; attempt status is unavailable.',rendered)
        self.assertNotIn('Alignerr: collection failed',rendered)
        self.assertNotIn('micro1: collection failed',rendered)

    def test_supported_stages_are_not_all_crawler_failures(self):
        cases=[('failed',2,{},'collection'),('failed',0,{},'coverage'),
            ('blocked',0,{},'coverage'),('not_started',0,{},'coverage'),
            ('disabled',0,{},'coverage'),('accounting_unavailable',None,{},'accounting'),
            ('receipt_finalization_failed',1,{},'accounting'),
            ('partial_or_failed',1,{'observed':0},'qualification'),
            ('failed',1,{'publication_outcome':'failed'},'publication'),
            ('collected_unpublished',1,{},'publication'),
            ('complete',1,{'qualifying_observation':True},None)]
        for outcome,requests,extra,expected in cases:
            with self.subTest(outcome=outcome,extra=extra):
                self.assertEqual(daily.source_condition(self.row('appen',outcome,requests,**extra)),expected)

    def test_historical_request_does_not_establish_current_attempt(self):
        plan=dict(version=maintenance.VERSION,source='alignerr',run_id='20260925T060000Z')
        plan['plan_id']=maintenance.digest(plan);journal=maintenance.Journal(self.config['journal'],plan)
        journal.append('source_transport',dict(event='request',observed_at='2026-09-25T06:00:04Z'))
        journal.append('source_transport',dict(event='transport_error',status=403))
        journal.append('finished',dict(status='collection_failed'))
        row=self.row('alignerr','accounting_unavailable',None,last_failed_collection_plan_id=plan['plan_id'])
        self.assertEqual(daily._source_attempt(self.config,row),{})
        self.assertEqual(daily._source_attempt(self.config,row,historical=True)['status'],403)
        row['collection_plan_id']=plan['plan_id']
        self.assertEqual(daily._source_attempt(self.config,row),{})

    def test_reclassification_preserves_history_without_false_recovery_or_repeat(self):
        self.install()
        legacy=dict(id='a'*32,key='alignerr:collection',kind='opened',at='2026-09-26T06:40:00Z',
            issue=dict(severity='error',reason='accounting_unavailable'),delivery='accepted_by_adapter')
        old=dict(checked_at='2026-09-26T06:40:00Z',active={'alignerr:collection':legacy['issue']},events=[legacy])
        daily.write_json(self.root/'health.json',old)
        with patch.object(daily,'_baseline_cohorts',return_value={}):
            state=daily.health(self.config,self.at)
            again=daily.health(self.config,self.at)
        self.assertEqual(state['events'][0],legacy)
        alignerr=[e for e in state['events'][1:] if e['key'].startswith('alignerr:')]
        self.assertEqual([(e['kind'],e['key']) for e in alignerr],[('status_changed','alignerr:accounting')])
        self.assertEqual(again['events'],state['events'])
        self.assertFalse(any(e['kind']=='recovered' for e in state['events'][1:]))

    def test_interrupted_correction_has_one_visibility_boundary_and_notification(self):
        from wahojobs import daily_receipt_reconciliation as recovery
        rows=self.install();original=daily.read_json(self.target/'run.json')
        rows=deepcopy(rows)
        for provider in ('dataforce','oneforma','welocalize'):
            rows[provider].update(outcome='complete',qualifying_observation=True,
                ended_at=original['ended_at'],reconciliation_proof={'legacy_transaction':True})
        for provider in ('alignerr','handshake','mercor','meridial','surge'):
            rows[provider].update(outcome='publication_failed',publication_outcome='not_started',requests_used=1)
        rows['mindrift'].update(outcome='qualification_failed',qualification_outcome='rejected',publication_outcome='failed')
        correction=dict(version=recovery.VERSION,run_id=self.run_id,original_receipt_sha256=maintenance.digest(original),sources=rows)
        save=maintenance.save_json
        def interrupted(path,value):
            if path.name=='current-reconciliation.json':raise OSError('fixture interruption before visibility')
            return save(path,value)
        with patch.object(daily,'_baseline_cohorts',return_value={}):
            previous=daily.health(self.config,self.at)
            with patch.object(recovery,'build',return_value=correction),patch.object(maintenance,'save_json',interrupted):
                with self.assertRaises(OSError):recovery.apply(self.config,self.run_id,authorized=True)
            hidden=daily.health(self.config,self.at+timedelta(minutes=1))
            self.assertEqual(hidden['context']['cycle'],previous['context']['cycle'])
            self.assertEqual(hidden['events'],previous['events'])
            with patch.object(recovery,'build',side_effect=AssertionError('prepared correction must be reused')):
                result=recovery.apply(self.config,self.run_id,authorized=True)
                self.assertEqual(recovery.apply(self.config,self.run_id,authorized=True),result)
            state=daily.health(self.config,self.at+timedelta(minutes=2))
            again=daily.health(self.config,self.at+timedelta(minutes=3))
        self.assertEqual(state['events'],again['events'])
        self.assertEqual(state['events'][:len(previous['events'])],previous['events'])
        added=state['events'][len(previous['events']):]
        self.assertFalse(any(e['kind'] in ('recovered','first_verified') for e in added))
        self.assertEqual(sum(e['key']=='run:accounting_reconciled' for e in added),1)
        self.assertEqual(len(list((self.target/'reconciliations').glob('*.json'))),1)
        self.assertEqual(daily.read_json(self.target/'run.json'),original)
        self.assertFalse(daily.read_json(self.root/'dataforce-state.json')['qualifying_observation'])
        self.assertTrue(daily.read_source_state(self.config,'dataforce')['qualifying_observation'])
        self.assertIn('mindrift:qualification',state['active'])
        self.assertIn('alignerr:publication',state['active'])
        rendered=email.message([e for e in added if e['delivery']=='pending'],state['context'])['text']
        self.assertIn('No new collection or verification renewal occurred',rendered)
        self.assertNotIn('Alignerr: collection failed',rendered)
        self.assertNotIn('Mindrift: collection failed',rendered)
        # A later native run supersedes the immutable historical overlay.
        newer=self.row('dataforce','failed',1);newer['run_id']='20260927T060000Z'
        daily.write_json(self.root/'dataforce-state.json',newer)
        self.assertEqual(daily.read_source_state(self.config,'dataforce'),newer)


class CommitReceiptTests(unittest.TestCase):
    setUp=coverage.CoverageIntegrationTests.setUp

    def captured(self,name):
        at=T0+timedelta(days=1)
        with coverage.offline(at,coverage.Transport()):
            daily.collect_phase(self.config,name,'prepare')
            daily.collect_phase(self.config,name,'collect-appen')
            daily.collect_phase(self.config,name,'backup')
        return at,self.root/'state/runs'/name

    def receipt(self,name,at):return dict(run_id=name,trigger='timer',started_at=daily.stamp(at),ended_at=daily.stamp(at+timedelta(minutes=5)))

    def test_termination_after_commit_recovers_exact_receipt_without_replay(self):
        at,target=self.captured('committed')
        original=maintenance.Journal.append
        def terminate(journal,event,data):
            if event=='operation_result':raise SystemExit('fixture process termination after commit')
            return original(journal,event,data)
        with coverage.offline(at,coverage.Transport()),patch.object(maintenance.Journal,'append',terminate):
            with self.assertRaises(SystemExit):daily.collect_phase(self.config,'committed','publish-appen')
        before=self.db.read_bytes();receipt=self.receipt('committed',at)
        with coverage.offline(at+timedelta(hours=3),coverage.Transport()):
            first=daily.reconstruct_source_receipt(self.config,receipt,'appen')
            again=daily.reconstruct_source_receipt(self.config,receipt,'appen')
        self.assertTrue(first['qualifying_observation'],first)
        self.assertEqual(first,again);self.assertEqual(before,self.db.read_bytes())
        self.assertEqual(daily.parse(first['last_qualifying_verification']),at)
        self.assertEqual(first['requests_used'],1);self.assertEqual(first['publication_requests_used'],0)
        raw=maintenance.report(self.journal,first['plan_id'])
        self.assertEqual(raw['status'],'interrupted') # original evidence is not rewritten

    def test_prepared_receipt_before_failed_commit_never_establishes_qualification(self):
        at,target=self.captured('uncommitted');original=maintenance.Journal.append
        def fail_after_prepare(journal,event,data):
            result=original(journal,event,data)
            if event=='catalog_commit_prepared':raise TimeoutError('fixture before database commit')
            return result
        with coverage.offline(at,coverage.Transport()),patch.object(maintenance.Journal,'append',fail_after_prepare):
            daily.collect_phase(self.config,'uncommitted','publish-appen')
        row=daily.reconstruct_source_receipt(self.config,self.receipt('uncommitted',at),'appen')
        self.assertFalse(row['qualifying_observation'])
        self.assertEqual(row['publication_outcome'],'failed')
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT status FROM crawl_runs ORDER BY id DESC LIMIT 1').fetchone()[0],'failed')

    def test_missing_publication_journal_preserves_bound_collection(self):
        at,target=self.captured('missing')
        with coverage.offline(at,coverage.Transport()):
            plan=maintenance.build_plan(self.db,['appen'],http_limit=1,detail_limit=0,details=None,phase='source',daily_discovery=True)
        daily.write_json(target/'appen-plan.json',plan)
        row=daily.reconstruct_source_receipt(self.config,self.receipt('missing',at),'appen')
        self.assertEqual(row['outcome'],'accounting_unavailable')
        self.assertEqual(row['attempt_started'],'yes');self.assertEqual(row['requests_used'],1)
        self.assertEqual(row['capture_outcome'],'collected_unpublished')
        self.assertEqual(row['accounting_status'],'publication_receipt_unavailable')

    def test_altered_local_plan_cannot_change_reconstructed_baseline(self):
        at,target=self.captured('altered')
        with coverage.offline(at,coverage.Transport()):daily.collect_phase(self.config,'altered','publish-appen')
        plan=daily.read_json(target/'appen-plan.json');plan['sources'][0]['jobs']=[dict(job_id=999)]
        daily.write_json(target/'appen-plan.json',plan)
        row=daily.reconstruct_source_receipt(self.config,self.receipt('altered',at),'appen')
        self.assertFalse(row['qualifying_observation']);self.assertEqual(row['requests_used'],1)
        self.assertEqual(row['accounting_status'],'publication_receipt_unavailable')

    def test_atomic_journal_entry_never_exposes_a_partial_tail(self):
        at,target=self.captured('atomic')
        ref=daily.read_json(target/'appen-collection.json')['plan_id']
        report=maintenance.report(self.journal,ref);folder=self.journal/ref
        next_path=folder/('%06d.json'%(len(report['events'])+1))
        with patch.object(maintenance.os,'link',side_effect=OSError('fixture interrupted before atomic publication')):
            with self.assertRaises(OSError):maintenance.save_json(next_path,{'unfinished':'fixture'})
        self.assertFalse(next_path.exists())
        self.assertEqual(maintenance.report(self.journal,ref),report)
        existing=folder/'plan.json';before=existing.read_bytes()
        with self.assertRaises(FileExistsError):maintenance.save_json(existing,{'not':'a replacement'})
        self.assertEqual(existing.read_bytes(),before)

    def test_process_exit_on_each_side_of_commit_recovers_only_committed_transaction(self):
        script='''import json,os,sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from tests import test_daily_source_coverage as coverage
from wahojobs import daily_inventory as d,evidence_maintenance as m
config=json.loads(Path(sys.argv[1]).read_text());original=m.Journal.append
def crash(journal,event,data):
    result=original(journal,event,data) if event=="catalog_commit_prepared" else None
    if event==sys.argv[3]:os._exit(71)
    return result if event=="catalog_commit_prepared" else original(journal,event,data)
with coverage.offline(datetime.fromisoformat(sys.argv[4]),coverage.Transport()),patch.object(m.Journal,"append",crash):
    d.collect_phase(config,sys.argv[2],"publish-appen")
'''
        for name,event,qualified in [('before-exit','catalog_commit_prepared',False),('after-exit','operation_result',True)]:
            with self.subTest(event=event):
                at,target=self.captured(name)
                config=self.root/(name+'-config.json');config.write_text(json.dumps(self.config))
                child=subprocess.run([sys.executable,'-B','-c',script,str(config),name,event,at.isoformat()],
                    cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=60)
                self.assertEqual(child.returncode,71,child.stderr.decode())
                # The native recovery boundary finalizes SQLite rollback files
                # before the read-only receipt reconstruction runs.
                from wahojobs.sqlite_recovery import native_finalize
                native_finalize(self.db)
                before=self.db.read_bytes()
                with coverage.offline(at+timedelta(hours=3),coverage.Transport()):
                    row=daily.reconstruct_source_receipt(self.config,self.receipt(name,at),'appen')
                    self.assertEqual(row,daily.reconstruct_source_receipt(self.config,self.receipt(name,at),'appen'))
                self.assertEqual(row['qualifying_observation'],qualified,row)
                self.assertEqual(self.db.read_bytes(),before)
                if qualified:self.assertEqual(daily.parse(row['last_qualifying_verification']),at)

    def test_legacy_committed_failure_reconciles_without_rewriting_run_or_clock(self):
        from wahojobs import daily_receipt_reconciliation as recovery
        at,target=self.captured('legacy');original_run=maintenance.run_crawl;original_inspect=maintenance.inspect_source
        calls=[]
        def legacy_run(*args,**kwargs):
            kwargs.pop('before_lifecycle_commit',None)
            return original_run(*args,**kwargs)
        def old_reporting_failure(*args,**kwargs):
            calls.append(1)
            if len(calls)==3:raise TimeoutError('fixture legacy post-commit inspection deadline')
            return original_inspect(*args,**kwargs)
        with coverage.offline(at,coverage.Transport()),patch.object(maintenance,'run_crawl',legacy_run),\
                patch.object(maintenance,'inspect_source',old_reporting_failure):
            daily.collect_phase(self.config,'legacy','publish-appen')
        receipt=self.receipt('legacy',at);receipt.update(normal_service_resumed=True,outcome='partial_or_failed',
            sources={s:daily.empty_source(s,at,outcome='not_started') for s in daily.SOURCES})
        receipt['sources']['appen']=daily.read_json(target/'appen.json')
        self.assertFalse(receipt['sources']['appen']['qualifying_observation'])
        daily.write_json(target/'run.json',receipt);before=(target/'run.json').read_bytes();database=self.db.read_bytes()
        with coverage.offline(at+timedelta(hours=3),coverage.Transport()):
            result=recovery.apply(self.config,'legacy',authorized=True)
            repeat=recovery.apply(self.config,'legacy',authorized=True)
        self.assertEqual(result,repeat);self.assertEqual(before,(target/'run.json').read_bytes())
        self.assertEqual(database,self.db.read_bytes())
        row=result['sources']['appen'];self.assertTrue(row['qualifying_observation'],row)
        self.assertEqual(daily.parse(row['last_qualifying_verification']),at)
        self.assertEqual(row['accounting_status'],'reconciled_exact_transaction')
        self.assertEqual(len(list((target/'reconciliations').glob('*.json'))),1)


if __name__=='__main__':unittest.main()

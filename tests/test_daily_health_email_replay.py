"""Historical September 2026 alert replay and isolated future-state cases.

The fixture is a sanitized copy of retained commissioning/source receipts.
No employer, hosted storage, or real mail transport is used.
"""
from copy import deepcopy
from contextlib import closing
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch

from wahojobs import daily_inventory as daily,operational_email as email
from wahojobs import evidence_maintenance as maintenance
from scripts import daily_inventory as cli

EVIDENCE=json.loads((Path(__file__).parent/'fixtures/daily_health_historical_20260923.json').read_text())
KEY='re_isolated_health_replay_key'


class HistoricalAlertReplay(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='health-replay-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config=dict(state_directory=str(self.root),database=str(self.root/'product.sqlite3'),
                         journal=str(self.root/'journal'),first_run_at='2026-09-23T06:00:00Z',
                         sources=daily.default_sources())
        self.transport=Mock(return_value='49a3999c-0ce1-4ea6-ab68-afcd6dc2e794')

    def when(self,key):return daily.parse(EVIDENCE['dates'][key])

    def first(self):
        with patch.object(daily,'_baseline_cohorts',return_value=EVIDENCE['before_first_cohorts']):
            return daily.health(self.config,self.when('A'))

    def install_cycle(self):
        for source,row in EVIDENCE['source_states'].items():
            daily.write_json(self.root/(source+'-state.json'),row)
        target=self.root/'runs'/EVIDENCE['first_run']['run_id'];target.mkdir(parents=True)
        receipt=dict(EVIDENCE['first_run'],sources=EVIDENCE['source_states'])
        daily.write_json(target/'run.json',receipt)
        daily.write_json(target/'worker.json',EVIDENCE['worker'])

    def check(self,key):
        with patch.object(daily,'_source_attempt',return_value=EVIDENCE['micro1_attempt']),\
             patch.object(daily,'_baseline_cohorts',return_value={}):
            return daily.health(self.config,self.when(key))

    def deliver_test(self,state,at):
        events=[e for e in state['events'] if e['delivery']=='pending']
        if not events:return None
        packet=dict(version=2,application='wahojobs-beta',recipient=email.ALERT_RECIPIENT,
                    events=events,context=state['context'])
        email.send_packet(packet,KEY,self.root,transport=self.transport,at=at)
        for event in events:event['delivery']='accepted_by_adapter'
        daily.write_json(self.root/'health.json',state)
        return self.transport.call_args.args[0]

    def test_three_historical_snapshots_corrected_with_no_live_delivery(self):
        a=self.first();a_mail=self.deliver_test(a,self.when('A'))
        self.assertEqual(EVIDENCE['historical_email_counts']['A'],dict(opened=20,recovered=0))
        self.assertEqual(len([e for e in a['events'] if e['delivery']=='accepted_by_adapter']),6)
        self.assertEqual(len(a['active']),6) # five known exclusions and forty expired Mercor records
        self.assertEqual(a['context']['expired_records'],40)
        self.assertIn('First daily check scheduled',a_mail['subject'])
        self.assertIn('40 exact posting records',a_mail['text'])
        self.assertNotIn('no_stored_qualifying_state',a_mail['text'])

        self.install_cycle()
        b=self.check('B');b_mail=self.deliver_test(b,self.when('B'))
        b_events=[e for e in b['events'] if e['at']==daily.stamp(self.when('B'))]
        self.assertEqual(EVIDENCE['historical_email_counts']['B'],dict(opened=10,recovered=15))
        self.assertEqual(sum(e['kind']=='recovered' for e in b_events),0)
        self.assertEqual(sum(e['kind']=='first_verified' for e in b_events),9)
        self.assertEqual(sum(e['kind']=='opened' for e in b_events),1) # micro1, Mercor remains open
        self.assertNotIn('run:failed',b['active'])
        self.assertEqual(b['context']['cycle']['state'],'partial')
        self.assertEqual(b['context']['expired_records'],39)
        self.assertIn('Partial daily check',b_mail['subject'])
        self.assertIn('1,093 newly published opportunities',b_mail['text'])
        self.assertIn('HTTP 403',b_mail['text'])
        self.assertNotIn('RECOVERED:micro1',b_mail['text'])

        # The other Mercor cohort crosses 36 hours during intervening hourly
        # checks. That is one real warning, not a repeat of the older 39 records.
        intermediate=self.check_at(daily.parse('2026-09-23T11:40:01+00:00'))
        intermediate_events=[e for e in intermediate['events'] if e['at']=='2026-09-23T11:40:01+00:00'
                             and e['delivery']=='pending']
        self.assertEqual(len(intermediate_events),1)
        self.assertEqual(intermediate_events[0]['issue']['records'],10)
        self.deliver_test(intermediate,daily.parse('2026-09-23T11:40:01+00:00'))
        self.config['sources']['micro1']['enabled']=False
        c=self.check('C');c_mail=self.deliver_test(c,self.when('C'))
        c_events=[e for e in c['events'] if e['at']==daily.stamp(self.when('C'))]
        self.assertEqual(EVIDENCE['historical_email_counts']['C'],dict(opened=1,recovered=0))
        self.assertEqual([(e['kind'],e['key']) for e in c_events if e['delivery']!='not_applicable'],
                         [('status_changed','micro1:coverage')])
        self.assertEqual(c['active']['micro1:coverage']['reason'],'disabled_after_http_403')
        self.assertEqual(c['active']['micro1:coverage']['last_attempt_at'],EVIDENCE['micro1_attempt']['at'])
        self.assertIn('Source paused after HTTP 403',c_mail['subject'])
        self.assertIn('bounded validation',c_mail['text'])
        self.assertEqual(self.transport.call_count,4)
        repeated=self.check('C')
        self.assertEqual(len(repeated['events']),len(c['events']))

    def test_legacy_accepted_incidents_migrate_without_recovery_storm_or_resend(self):
        self.install_cycle()
        old=dict(checked_at=EVIDENCE['dates']['A'],active=deepcopy(EVIDENCE['legacy_pre_first_active']),
                 events=deepcopy(EVIDENCE['legacy_events'][:20]))
        daily.write_json(self.root/'health.json',old)
        new=self.check('B')
        self.assertEqual(new['events'][:20],old['events'])
        self.assertFalse(any(e['kind']=='recovered' for e in new['events'][20:]))
        self.assertFalse(any(e['key'].startswith('dataforce:collection') for e in new['events'][20:]))

        self.config['sources']['micro1']['enabled']=False
        legacy=dict(checked_at='2026-09-23T20:40:01+00:00',
                    active=deepcopy(EVIDENCE['legacy_after_first_active']),
                    events=deepcopy(EVIDENCE['legacy_events']))
        daily.write_json(self.root/'health.json',legacy)
        migrated=self.check('C')
        self.assertEqual(migrated['events'][:45],legacy['events'])
        pending=[e for e in migrated['events'][45:] if e['delivery']=='pending']
        self.assertEqual([(e['kind'],e['key']) for e in pending],[('status_changed','micro1:coverage')])
        self.assertFalse(any(e['kind']=='recovered' for e in migrated['events'][45:]))
        self.assertNotIn('mercor:age36',migrated['active'])
        self.assertNotIn('mercor:age48',migrated['active'])
        again=self.check('C');self.assertEqual(len(again['events']),len(migrated['events']))

    def test_missed_run_fatal_failure_expiry_escalation_and_genuine_resolution(self):
        self.first();self.install_cycle();b=self.check('B')
        self.assertNotIn('run:failed',b['active'])
        later=self.when('B')+timedelta(days=1,hours=1)
        missed=self.check_at(later)
        self.assertIn('run:missing',missed['active'])
        self.assertEqual(missed['active']['run:missing']['severity'],'error')
        # Synthetic next-day success is a separate scenario, not a historical email.
        next_run=(later+timedelta(days=1)).replace(hour=6,minute=0,second=0,microsecond=0)
        run_id=next_run.strftime('%Y%m%dT060000Z');target=self.root/'runs'/run_id;target.mkdir()
        sources=deepcopy(EVIDENCE['source_states'])
        sources['micro1'].update(qualifying_observation=True,outcome='complete',requests_used=4,
                                 ended_at=daily.stamp(next_run+timedelta(minutes=4)),
                                 cohorts=[dict(verified_at=daily.stamp(next_run),records=300)])
        daily.write_json(self.root/'micro1-state.json',sources['micro1'])
        receipt=dict(EVIDENCE['first_run'],run_id=run_id,outcome='complete',sources=sources,
                     scheduled_at=daily.stamp(next_run),ended_at=daily.stamp(next_run+timedelta(minutes=4)))
        daily.write_json(target/'run.json',receipt)
        resolved=self.check_at(later+timedelta(days=1,hours=1))
        self.assertTrue(any(e['kind']=='recovered' and e['key']=='micro1:collection' for e in resolved['events']))
        self.assertIn('mercor:cohort_',next(k for k in resolved['active'] if k.startswith('mercor:cohort_')))
        # A separate fatal outcome remains an error even if some source rows exist.
        receipt['outcome']='recovery_failed';receipt['normal_service_resumed']=False
        daily.write_json(target/'run.json',receipt)
        fatal=self.check_at(later+timedelta(days=1,hours=2))
        self.assertIn('run:failed',fatal['active'])

    def check_at(self,at):
        with patch.object(daily,'_source_attempt',return_value=EVIDENCE['micro1_attempt']),\
             patch.object(daily,'_baseline_cohorts',return_value={}):
            return daily.health(self.config,at)

    def test_delivery_failure_keeps_unsent_alert_visible_without_retry(self):
        a=self.first()
        config=dict(self.config,alert_delivery=dict(command=['/isolated/test-transport'],recipient=email.ALERT_RECIPIENT))
        with patch.object(cli.subprocess,'run',side_effect=TimeoutError('test transport unavailable')) as sender:
            cli.deliver(config,a)
        self.assertEqual(sender.call_count,1)
        self.assertTrue(any(e['delivery']=='failed_or_uncertain' for e in a['events']))
        next_state=self.check_at(self.when('A')+timedelta(hours=1))
        self.assertIn('delivery:uncertain',next_state['active'])
        self.assertTrue(any(e['kind']=='opened' and e['key']=='delivery:uncertain' for e in next_state['events']))
        self.assertEqual(sender.call_count,1)
        # A worker killed after its durable pre-dispatch claim is also uncertain.
        daily.write_json(self.root/'health.json',dict(active={},events=[dict(
            id='a'*32,kind='opened',key='mercor:cohort_pending',at=EVIDENCE['dates']['A'],
            delivery='attempted')]))
        crashed=self.check_at(self.when('A')+timedelta(hours=2))
        self.assertIn('delivery:uncertain',crashed['active'])

    def test_real_journal_status_survives_later_disabled_cycle(self):
        plan=dict(version=maintenance.VERSION,source='micro1',run_id=EVIDENCE['source_states']['micro1']['run_id'])
        plan['plan_id']=maintenance.digest(plan)
        journal=maintenance.Journal(self.config['journal'],plan)
        journal.append('source_transport',dict(event='request',observed_at=EVIDENCE['micro1_attempt']['at']))
        journal.append('source_transport',dict(event='transport_error',status=403,error_type='HTTPError'))
        journal.append('finished',dict(status='collection_failed'))
        failed=deepcopy(EVIDENCE['source_states']['micro1'])
        failed['collection_plan_id']=plan['plan_id']
        self.assertEqual(daily._source_attempt(self.config,failed)['status'],403)
        disabled=daily.empty_source('micro1',self.when('C'),outcome='disabled',reason='owner_configuration_disabled')
        disabled=daily.merge_source_history(disabled,failed)
        self.assertEqual(disabled['last_failed_collection_plan_id'],plan['plan_id'])
        disabled=daily.merge_source_history(daily.empty_source('micro1',self.when('C')+timedelta(days=1),
            outcome='disabled',reason='owner_configuration_disabled'),disabled)
        daily.write_json(self.root/'micro1-state.json',disabled)
        self.config['sources']['micro1']['enabled']=False
        with patch.object(daily,'_baseline_cohorts',return_value={}):
            issues=daily.health_issues(self.config,self.when('C'))
        self.assertEqual(issues['micro1:coverage']['reason'],'disabled_after_http_403')
        self.assertEqual(issues['micro1:coverage']['http_status'],403)
        # Hosted old code can have already written a disabled row without the
        # new provenance field before this release is deployed.
        old=self.root/'runs'/'20260923T060000Z';old.mkdir(parents=True)
        daily.write_json(old/'micro1.json',failed)
        legacy=daily.empty_source('micro1',self.when('C')+timedelta(days=2),
            outcome='disabled',reason='owner_configuration_disabled')
        daily.write_json(self.root/'micro1-state.json',legacy)
        with patch.object(daily,'_baseline_cohorts',return_value={}):
            legacy_issues=daily.health_issues(self.config,self.when('C'))
        self.assertEqual(legacy_issues['micro1:coverage']['reason'],'disabled_after_http_403')
        future='20260925T060000Z';future_dir=self.root/'runs'/future;future_dir.mkdir()
        daily.write_json(future_dir/'run.json',dict(trigger='isolated_worker'))
        daily.save_source(self.config,future,daily.empty_source('micro1',self.when('C')+timedelta(days=2),
            outcome='disabled',reason='owner_configuration_disabled'))
        anchored=daily.read_json(self.root/'micro1-state.json')
        self.assertEqual(anchored['last_failed_collection_plan_id'],plan['plan_id'])

    def test_failed_first_attempt_preserves_authoritative_expired_cohort(self):
        with closing(sqlite3.connect(self.config['database'])) as db:
            db.executescript('''CREATE TABLE companies(id INTEGER PRIMARY KEY,slug TEXT);
                CREATE TABLE jobs(id INTEGER PRIMARY KEY,company_id INTEGER,is_active INTEGER);
                CREATE TABLE crawl_runs(id INTEGER PRIMARY KEY,company_id INTEGER,status TEXT,
                    used_sample_data INTEGER,error_message TEXT,started_at TEXT,finished_at TEXT);
                CREATE TABLE job_source_content_captures(id INTEGER PRIMARY KEY,job_id INTEGER,
                    crawl_run_id INTEGER,provider TEXT,source_type TEXT,record_promotion_contract_id TEXT,
                    promotion_policy_version TEXT,promotion_decision TEXT,decision_reasons_json TEXT,
                    used_sample_data INTEGER,provider_outcome TEXT,normalized_record_count INTEGER,
                    candidate_count INTEGER,observed_at TEXT);''')
            db.execute("INSERT INTO companies VALUES(1,'mercor')")
            db.execute('INSERT INTO jobs VALUES(1,1,1)')
            db.execute("INSERT INTO crawl_runs VALUES(1,1,'partial',0,NULL,?,?)",
                ('2026-09-18T13:07:32+00:00',)*2)
            db.execute('''INSERT INTO job_source_content_captures VALUES
                (1,1,1,'mercor','mercor-marketplace','mercor_public_active_record_v1',
                 'mercor_record_promotion_v1','promoted',NULL,0,'partial',1,1,?)''',
                 ('2026-09-18T13:07:32+00:00',))
            db.commit()
        cohort=daily._baseline_cohorts(self.config,['mercor'])['mercor']
        self.assertEqual(cohort,[dict(verified_at='2026-09-18T13:07:32+00:00',records=1)])
        failed=daily.empty_source('mercor',self.when('B'),outcome='collection_failed_or_interrupted')
        failed['requests_used']=1  # This scenario includes one dispatched attempt.
        daily.write_json(self.root/'mercor-state.json',failed)
        issues=daily.health_issues(self.config,self.when('B'))
        self.assertIn('mercor:collection',issues)
        self.assertEqual(next(v['records'] for k,v in issues.items() if k.startswith('mercor:cohort_')),1)

    def test_malformed_partial_worker_receipt_is_reported_as_failed(self):
        self.install_cycle()
        target=self.root/'runs'/EVIDENCE['first_run']['run_id']
        (target/'worker.json').write_text('{unfinished',encoding='utf-8')
        issues=daily.health_issues(self.config,self.when('B'))
        self.assertEqual(issues['run:failed']['reason'],'failed_publication_or_recovery')
        daily.write_json(target/'worker.json',[])
        self.assertIn('run:failed',daily.health_issues(self.config,self.when('B')))
        receipt=daily.read_json(target/'run.json');receipt['sources']=[]
        daily.write_json(target/'run.json',receipt)
        self.assertIn('run:failed',daily.health_issues(self.config,self.when('B')))
        receipt['sources']={'mercor':[]}
        daily.write_json(target/'run.json',receipt)
        self.assertIn('run:failed',daily.health_issues(self.config,self.when('B')))
        self.assertIn('run:failed',daily.health(self.config,self.when('B'))['active'])
        receipt['sources']={};receipt['outcome']='complete'
        daily.write_json(target/'run.json',receipt)
        self.assertEqual(daily.health(self.config,self.when('B'))['active']['run:failed']['reason'],
                         'invalid_source_receipt')

    def test_process_crash_after_durable_delivery_claim_is_alerted(self):
        a=self.first()
        config=dict(self.config,alert_delivery=dict(command=['/isolated/test-transport'],recipient=email.ALERT_RECIPIENT))
        with patch.object(cli.subprocess,'run',side_effect=SystemExit(7)):
            with self.assertRaises(SystemExit):cli.deliver(config,a)
        ledger=daily.read_json(self.root/'health.json')
        self.assertTrue(any(e['delivery']=='attempted' for e in ledger['events']))
        with patch.object(daily,'_baseline_cohorts',return_value=EVIDENCE['before_first_cohorts']):
            issues=daily.health_issues(self.config,self.when('A')+timedelta(hours=1))
        self.assertIn('delivery:uncertain',issues)

    def test_reenable_without_new_verification_is_not_recovery(self):
        prior=self.when('B')+timedelta(hours=2)
        state=deepcopy(EVIDENCE['source_states']['alignerr'])
        state['provider']='micro1'
        daily.write_json(self.root/'micro1-state.json',state)
        daily.write_json(self.root/'health.json',dict(checked_at=daily.stamp(prior),events=[],
            active={'micro1:coverage':dict(reason='configured_disabled',severity='warning')}))
        with patch.object(daily,'_baseline_cohorts',return_value={}):
            result=daily.health(self.config,prior+timedelta(hours=1))
        self.assertFalse(any(e['kind']=='recovered' and e['key']=='micro1:coverage' for e in result['events']))
        self.assertTrue(any(e['kind']=='reclassified' and e['key']=='micro1:coverage' for e in result['events']))


if __name__=='__main__':unittest.main()

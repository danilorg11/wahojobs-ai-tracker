"""A real committed verification can explain a later no-request cooldown skip."""
from contextlib import closing
from datetime import timedelta
import sqlite3
import unittest
from unittest.mock import patch

from wahojobs import daily_inventory as daily, operational_email
from tests import test_daily_source_coverage as coverage
from tests.evidence_maintenance_support import T0


class VerifiedCooldownTests(unittest.TestCase):
    def setUp(self):
        coverage.CoverageIntegrationTests.setUp(self)
        for name,row in self.config['sources'].items():row['enabled']=name=='mindrift'
        self.at=T0+timedelta(days=1)
        with coverage.offline(self.at,coverage.Transport()):
            for phase in ('prepare','collect-mindrift','backup','publish-mindrift','finish'):
                daily.collect_phase(self.config,'retained-success',phase)
        self.original=daily.read_source_state(self.config,'mindrift')
        self.assertTrue(self.original['qualifying_observation'])
        self.skipped=self.at+timedelta(hours=1)
        with coverage.offline(self.skipped,coverage.Transport()):
            self.repair=daily.reserve_repair_run(self.config,self.skipped,'fixture-cooldown-repair',['mindrift'])
            daily.collect_phase(self.config,self.repair['run_id'],'prepare')
        self.row=daily.read_source_state(self.config,'mindrift')
        self.rootstate=self.root/'state'

    def finish(self,rows=None):
        self.repair.update(outcome='partial_or_failed',sources=rows or {'mindrift':self.row},
            ended_at=daily.stamp(self.skipped+timedelta(minutes=1)),normal_service_resumed=True)
        target=self.rootstate/'runs'/self.repair['run_id']
        daily.write_json(target/'run.json',self.repair)
        daily.write_json(target/'worker.json',dict(completed=True,protected_domains_unchanged=True))

    def test_actual_proof_survives_release_change_and_interval_end_without_renewal(self):
        self.config['code_commit']='b'*40
        before=self.db.read_bytes();original=(self.rootstate/'runs/retained-success/mindrift.json').read_bytes()
        skipbytes=(self.rootstate/'mindrift-state.json').read_bytes()
        for at in (self.skipped,self.at+timedelta(hours=13)):
            proof=daily.verified_cooldown(self.config,self.row,at)
            self.assertIsNotNone(proof)
            self.assertEqual(proof['verified_at'],self.original['last_qualifying_verification'])
            self.assertNotIn('mindrift:coverage',daily.health_issues(self.config,at))
        self.assertFalse(self.row['qualifying_observation']);self.assertEqual(self.row['requests_used'],0)
        self.assertEqual(self.db.read_bytes(),before)
        self.assertEqual((self.rootstate/'runs/retained-success/mindrift.json').read_bytes(),original)
        self.assertEqual((self.rootstate/'mindrift-state.json').read_bytes(),skipbytes)

    def test_missing_failed_tampered_or_expired_evidence_stays_actionable(self):
        prior=self.rootstate/'runs/retained-success/mindrift.json';original=prior.read_bytes()
        for changed in (dict(self.original,qualifying_observation=False),dict(self.original,cohorts=[])):
            daily.write_json(prior,changed)
            self.assertIsNone(daily.verified_cooldown(self.config,self.row,self.skipped))
        prior.write_bytes(original)
        self.assertIsNone(daily.verified_cooldown(self.config,self.row,self.at+timedelta(hours=72)))
        for change in (dict(reason='other'),dict(next_eligible_at=daily.stamp(self.at+timedelta(hours=15))),
                dict(requests_used=1),dict(cohorts=[dict(self.row['cohorts'][0],records=999)])):
            self.assertIsNone(daily.verified_cooldown(self.config,dict(self.row,**change),self.skipped))
        with closing(sqlite3.connect(self.db)) as db:
            latest=db.execute("SELECT * FROM crawl_runs WHERE company_id=(SELECT id FROM companies WHERE slug='mindrift') ORDER BY id DESC LIMIT 1")
            columns=[d[0] for d in latest.description];row=dict(zip(columns,latest.fetchone()));row.pop('id')
            row.update(status='failed',started_at=daily.stamp(self.at-timedelta(hours=1)),error_message='fixture failure')
            db.execute('INSERT INTO crawl_runs ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',list(row.values()));db.commit()
        self.assertIsNone(daily.verified_cooldown(self.config,self.row,self.skipped))
        self.assertIn('mindrift:coverage',daily.health_issues(self.config,self.skipped))

    def test_reconstructed_skip_uses_original_schedule_without_rewriting_receipt(self):
        receipt=dict(self.repair,ended_at=daily.stamp(self.skipped))
        row=daily.merge_source_history(daily.reconstruct_source_receipt(self.config,receipt,'mindrift'),self.original)
        self.assertNotIn('next_eligible_at',row)
        self.assertIsNotNone(daily.verified_cooldown(self.config,row,self.skipped))

    def test_health_reclassifies_false_alert_once_and_uses_one_proof_per_invocation(self):
        self.finish()
        false=dict(key='mindrift:coverage',kind='opened',id='a'*32,at=daily.stamp(self.skipped),delivery='pending',
            issue=dict(severity='warning',reason='cooldown',corrective_action='Resolve the recorded eligibility or source blocker before claiming a check.'))
        daily.write_json(self.rootstate/'health.json',dict(checked_at=daily.stamp(self.skipped),active={'mindrift:coverage':false['issue']},
            events=[false],context=dict(qualified_sources=[],checked_at=daily.stamp(self.skipped))))
        with patch.object(daily,'reconstruct_source_receipt',wraps=daily.reconstruct_source_receipt) as reader:
            result=daily.health(self.config,self.skipped+timedelta(minutes=2))
        self.assertEqual(reader.call_count,1)
        self.assertNotIn('mindrift:coverage',result['active'])
        self.assertNotIn('mindrift',result['context']['qualified_sources'])
        self.assertEqual(result['context']['recently_verified_sources'],['mindrift'])
        self.assertFalse(any(e['kind'] in ('recovered','first_verified') and e['key'].startswith('mindrift:') for e in result['events']))
        rendered=operational_email.message([false],result['context'])['text']
        self.assertIn('recently verified source deferred',rendered)
        self.assertIn('0 of 1 selected sources verified and published in this repair',rendered)
        self.assertNotIn('Repair sources still requiring verification: Mindrift',rendered)
        self.assertNotIn('Resolve the recorded eligibility',rendered)
        late=daily.health(self.config,self.at+timedelta(hours=13))
        self.assertEqual(late['context']['recently_verified_sources'],['mindrift'])
        self.assertIn('minimum interval has elapsed',operational_email.message([false],late['context'])['text'])
        # An actual later collection keeps the original first-verification identity.
        at=self.at+timedelta(days=1)
        with coverage.offline(at,coverage.Transport()):
            for phase in ('prepare','collect-mindrift','backup','publish-mindrift','finish'):
                daily.collect_phase(self.config,'next-genuine-check',phase)
        after=daily.health(self.config,at+timedelta(minutes=1))
        self.assertFalse(any(e['kind']=='first_verified' and e['key']=='mindrift:verification' for e in after['events']))

    def test_recent_skip_does_not_hide_pending_other_source(self):
        self.config['sources']['mercor']['enabled']=True
        at=self.skipped+timedelta(minutes=2)
        with coverage.offline(at,coverage.Transport()):
            self.repair=daily.reserve_repair_run(self.config,at,'fixture-pending-repair',['mindrift','mercor'])
            daily.collect_phase(self.config,self.repair['run_id'],'prepare')
        self.skipped=at;self.row=daily.read_source_state(self.config,'mindrift')
        mercor=daily.empty_source('mercor',at,outcome='partial_individual')
        mercor.update(qualifying_observation=True,pending_qualification_count=40,run_id=self.repair['run_id'])
        self.finish({'mindrift':self.row,'mercor':mercor})
        view=daily.latest_operator_repair(self.config,at+timedelta(minutes=2))
        self.assertEqual(view['qualified_sources'],['mercor']);self.assertEqual(view['recently_verified_sources'],['mindrift'])
        self.assertEqual(view['remaining_sources'],[]);self.assertEqual(view['incomplete_sources'],['mercor'])
        self.assertEqual(view['state'],'partial');self.assertFalse(view['resolves_daily_failure'])

    def test_mixed_fresh_and_recent_repair_keeps_counts_and_resolves_only_current_coverage(self):
        self.config['sources']['appen']['enabled']=True
        at=self.skipped+timedelta(minutes=2)
        with coverage.offline(at,coverage.Transport()):
            self.repair=daily.reserve_repair_run(self.config,at,'fixture-mixed-repair',['mindrift','appen'])
            for phase in ('prepare','collect-appen','backup','publish-appen','finish'):
                daily.collect_phase(self.config,self.repair['run_id'],phase)
        self.skipped=at;self.row=daily.read_source_state(self.config,'mindrift')
        appen=daily.read_source_state(self.config,'appen')
        self.finish({'mindrift':self.row,'appen':appen})
        self.assertLess(daily.parse(appen['ended_at']),daily.parse(self.repair['ended_at']))
        view=daily.latest_operator_repair(self.config,at+timedelta(minutes=2))
        self.assertEqual(view['qualified_sources'],['appen'])
        self.assertEqual(view['recently_verified_sources'],['mindrift'])
        self.assertEqual(view['state'],'complete');self.assertTrue(view['resolves_daily_failure'])
        self.assertEqual(daily.read_json(self.rootstate/'runs'/self.repair['run_id']/'run.json')['outcome'],'partial_or_failed')

    def test_prior_slot_success_explains_cooldown_but_cannot_resolve_new_failed_daily_slot(self):
        at=self.at+timedelta(days=1,hours=9)  # 23:00; next eligible 11:00 tomorrow.
        with coverage.offline(at,coverage.Transport()):
            for phase in ('prepare','collect-mindrift','backup','publish-mindrift','finish'):
                daily.collect_phase(self.config,'prior-slot-success',phase)
        skipped=at+timedelta(hours=8)  # 07:00, after a newer 06:00 daily slot.
        with coverage.offline(skipped,coverage.Transport()):
            self.repair=daily.reserve_repair_run(self.config,skipped,'fixture-prior-slot-repair',['mindrift'])
            daily.collect_phase(self.config,self.repair['run_id'],'prepare')
        self.skipped=skipped;self.row=daily.read_source_state(self.config,'mindrift');self.finish()
        view=daily.latest_operator_repair(self.config,skipped+timedelta(minutes=2))
        self.assertEqual(view['recently_verified_sources'],['mindrift'])
        self.assertFalse(view['resolves_daily_failure'])
        self.assertLess(daily.parse(view['recent_verifications']['mindrift']['verified_at']),daily.slot_at(skipped))

    def test_urgent_health_never_opens_cooldown_proof(self):
        with patch.object(daily,'_verified_cooldown',side_effect=AssertionError('urgent storage read')):
            daily.health(self.config,self.skipped,application_ready=False,urgent=True)


if __name__=='__main__':unittest.main()

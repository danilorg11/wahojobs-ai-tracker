"""Explicit fresh repairs preserve consumed daily slots and all safety gates."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import daily_inventory as cli
from wahojobs import daily_inventory as daily, operational_email
from tests import test_daily_source_coverage as coverage
from tests.evidence_maintenance_support import T0

AT=datetime(2026,9,27,22,tzinfo=timezone.utc)


class RepairPolicyTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.config=dict(state_directory=str(self.root),database=str(self.root/'product.sqlite3'),
            journal=str(self.root/'journal'),sources=daily.default_sources(),code_commit='a'*40,
            first_run_at='2026-09-23T06:00:00+00:00')
        self.config['sources']['micro1']['enabled']=False

    def reserve(self,request='inventory-repair-20260927',sources=None,at=AT):
        return daily.reserve_repair_run(self.config,at,request,sources or ['appen'])

    def test_request_is_global_once_only_and_original_daily_slot_is_immutable(self):
        original=daily.reserve_run(self.root,AT.replace(hour=6),AT.replace(hour=6),'timer')
        path=self.root/'runs'/original['run_id']/'run.json'
        original.update(outcome='failed',ended_at=daily.stamp(AT.replace(hour=6,minute=5)))
        daily.write_json(path,original);before=path.read_bytes()
        repair=self.reserve()
        self.assertNotEqual(repair['run_id'],original['run_id'])
        self.assertEqual(repair['trigger'],'operator_repair')
        self.assertEqual(repair['code_commit'],self.config['code_commit'])
        self.assertEqual(repair['started_at'],daily.stamp(AT))
        self.assertIsNone(self.reserve(at=AT+timedelta(days=1)))
        self.assertEqual(path.read_bytes(),before)
        tomorrow=daily.reserve_run(self.root,(AT+timedelta(days=1)).replace(hour=6),AT.replace(hour=6),'timer')
        self.assertEqual(tomorrow['run_id'],'20260928T060000Z')
        with self.assertRaisesRegex(ValueError,'already_bound'):self.reserve(sources=['rws'])
        self.config['code_commit']='b'*40
        with self.assertRaisesRegex(ValueError,'already_bound'):self.reserve()

    def test_disabled_unbounded_duplicate_or_implicit_scope_is_rejected_before_claim(self):
        for request,sources in [('bad/path',['appen']),('valid-request',['micro1']),
                ('valid-request',['appen','appen']),('valid-request',[]),('valid-request',None)]:
            with self.subTest(request=request,sources=sources),self.assertRaises(ValueError):
                daily.reserve_repair_run(self.config,AT,request,sources)
        self.assertFalse((self.root/'repair-requests').exists())
        operations=Mock()
        with self.assertRaisesRegex(ValueError,'explicit_manual'):
            cli.supervise(self.config,'policy','auto',operations=operations,
                repair_request='valid-request',repair_sources=['appen'])
        operations.preflight.assert_not_called()

    def test_policy_changes_after_reservation_cannot_widen_worker_scope(self):
        receipt=self.reserve()
        self.config['sources']['appen']['http_max']+=1
        with self.assertRaisesRegex(ValueError,'release_or_policy_changed'):
            daily.collect_phase(self.config,receipt['run_id'],'prepare')
        self.assertFalse((self.root/'runs'/receipt['run_id']/'coverage-plan.json').exists())

    def test_repair_worker_still_requires_active_bound_parent_and_exclusive_phase_claim(self):
        receipt=self.reserve();target=self.root/'runs'/receipt['run_id']/'run.json'
        with self.assertRaisesRegex(ValueError,'active_supervisor'):
            cli.claim_dispatch(self.root,receipt['run_id'],123,0,'collect-appen')
        receipt.update(outcome='running',supervisor_pid=123,execution_deadline_monotonic=60,
            active_phase=dict(name='collect-appen',deadline=40))
        daily.write_json(target,receipt)
        self.assertEqual(cli.claim_dispatch(self.root,receipt['run_id'],123,10,'collect-appen'),30)
        with self.assertRaises(FileExistsError):cli.claim_dispatch(self.root,receipt['run_id'],123,10,'collect-appen')

    def test_selected_repair_preserves_unselected_state_and_actual_time_order(self):
        receipt=self.reserve();target=self.root/'runs'/receipt['run_id']
        untouched=self.root/'rws-state.json';daily.write_json(untouched,dict(sentinel=True))
        before=untouched.read_bytes()
        plan={s:dict(state='due',reason=None,next_eligible_at=None) for s in daily.SOURCES}
        with patch.object(daily,'coverage_plan',return_value=plan):
            daily.collect_phase(self.config,receipt['run_id'],'prepare')
        self.assertEqual(daily.read_json(target/'coverage-plan.json')['rws']['state'],'not_targeted')
        with self.assertRaisesRegex(ValueError,'outside_targeted'):
            daily.collect_phase(self.config,receipt['run_id'],'collect-rws')
        later=daily.empty_source('appen',AT+timedelta(minutes=1),outcome='complete')
        later.update(run_id='00000000-lexically-earlier-but-actually-newer',qualifying_observation=True)
        daily.write_json(self.root/'appen-state.json',later);newer=(self.root/'appen-state.json').read_bytes()
        daily.finish_run_sources(self.config,receipt)
        self.assertEqual(set(receipt['sources']),{'appen'})
        self.assertEqual(untouched.read_bytes(),before)
        self.assertEqual((self.root/'appen-state.json').read_bytes(),newer)

    def test_failed_repair_restores_normally_and_repeated_request_dispatches_no_collection(self):
        operations=Mock();operations.publish.side_effect=TimeoutError('fixture failure')
        with patch.object(daily,'now',return_value=AT):
            receipt=cli.supervise(self.config,'policy','manual',operations=operations,
                repair_request='inventory-repair-20260927',repair_sources=['appen'])
            duplicate=cli.supervise(self.config,'policy','manual',operations=operations,
                repair_request='inventory-repair-20260927',repair_sources=['appen'])
        operations.restore.assert_called_once_with(daily.RECOVERY_SECONDS)
        self.assertTrue(receipt['normal_service_resumed'])
        self.assertEqual(receipt['outcome'],'failed')
        self.assertEqual(operations.collect.call_count,1)
        self.assertLessEqual(operations.collect.call_args.args[1],daily.execution_seconds(self.config))
        self.assertEqual(duplicate['outcome'],'already_consumed_or_not_due')

    def test_cli_all_enabled_is_explicit_and_keeps_disabled_sources_excluded(self):
        with patch.object(cli,'private_policy',return_value=self.config),patch.object(daily,'validate_policy'),\
                patch.object(cli,'supervise',return_value={'outcome':'already_consumed_or_not_due'}) as supervisor,\
                patch.object(cli.signal,'signal'):
            self.assertEqual(cli.main(['run','--policy','fixture','--trigger','manual',
                '--repair-request','inventory-repair-20260927','--all-enabled']),0)
        selected=supervisor.call_args.kwargs['repair_sources']
        self.assertNotIn('micro1',selected)
        self.assertEqual(set(selected),{s for s,row in self.config['sources'].items() if row['enabled']})
        with patch.object(cli,'private_policy') as policy,self.assertRaisesRegex(ValueError,'explicit_manual'):
            cli.main(['run','--policy','fixture','--repair-request','inventory-repair-20260927','--all-enabled'])
        policy.assert_not_called()

    def test_successful_repair_resolves_current_failure_but_preserves_historical_daily_email(self):
        for source in self.config['sources']:self.config['sources'][source]['enabled']=source in ('appen','rws')
        path=self.root/'runs/20260927T060000Z/run.json'
        daily.write_json(path,dict(run_id='20260927T060000Z',outcome='failed',scheduled_at='2026-09-27T06:00:00+00:00',sources={}))
        before=path.read_bytes()
        with patch.object(daily,'_baseline_cohorts',return_value={}):initial=daily.health(self.config,AT)
        self.assertIn('run:failed',initial['active'])
        repair=self.reserve(sources=['appen','rws'],at=AT+timedelta(minutes=1))
        target=self.root/'runs'/repair['run_id'];ended=AT+timedelta(minutes=2)
        rows={}
        for source in ('appen','rws'):
            row=daily.empty_source(source,ended,outcome='complete')
            row.update(run_id=repair['run_id'],qualifying_observation=True,requests_used=1,
                ended_at=daily.stamp(ended),
                last_qualifying_verification=daily.stamp(ended),cohorts=[dict(verified_at=daily.stamp(ended),records=1)])
            rows[source]=row;daily.write_json(self.root/(source+'-state.json'),row)
        repair.update(outcome='complete_with_coverage_gaps',sources=rows,ended_at=daily.stamp(ended),normal_service_resumed=True)
        daily.write_json(target/'run.json',repair);daily.write_json(target/'worker.json',dict(protected_domains_unchanged=True))
        with patch.object(daily,'_baseline_cohorts',return_value={}):state=daily.health(self.config,ended+timedelta(minutes=1))
        self.assertNotIn('run:failed',state['active'])
        self.assertEqual(state['context']['cycle']['state'],'failed')
        self.assertEqual(state['context']['operator_repair']['state'],'complete')
        rendered=operational_email.message(state['events'],state['context'])
        self.assertIn('Inventory verified by operator repair',rendered['subject'])
        self.assertIn('The original scheduled-cycle receipt remains unchanged.',rendered['text'])
        self.assertEqual(path.read_bytes(),before)
        # Historical success cannot mask a later unverified source attempt.
        daily.write_json(self.root/'appen-state.json',dict(rows['appen'],qualifying_observation=False,
            ended_at=daily.stamp(ended+timedelta(seconds=1))))
        self.assertFalse(daily.latest_operator_repair(self.config,ended+timedelta(minutes=1))['resolves_daily_failure'])
        daily.write_json(self.root/'appen-state.json',rows['appen'])
        self.config['sources']['turing']['enabled']=True
        with patch.object(daily,'_baseline_cohorts',return_value={}):partial=daily.health_issues(self.config,ended+timedelta(minutes=2))
        self.assertIn('run:failed',partial)
        self.assertIn('turing:unverified',partial)

    def test_positive_partial_source_keeps_pending_ids_visible_until_a_fresh_complete_attempt(self):
        for source in self.config['sources']:self.config['sources'][source]['enabled']=source=='mercor'
        row=daily.empty_source('mercor',AT,outcome='partial_individual')
        row.update(qualifying_observation=True,requests_used=3,pending_qualification_count=2,
            ended_at=daily.stamp(AT),last_qualifying_verification=daily.stamp(AT))
        daily.write_json(self.root/'mercor-state.json',row)
        with patch.object(daily,'_baseline_cohorts',return_value={}):state=daily.health(self.config,AT)
        issue=state['active']['mercor:undercoverage']
        self.assertEqual(issue['pending_records'],2)
        self.assertEqual(daily.source_condition(row),'undercoverage')
        rendered=operational_email.message(state['events'],state['context'])
        self.assertIn('2 discovered or known posting IDs remain unverified',rendered['text'])
        row.update(pending_qualification_count=0,ended_at=daily.stamp(AT+timedelta(minutes=1)))
        daily.write_json(self.root/'mercor-state.json',row)
        with patch.object(daily,'_baseline_cohorts',return_value={}):resolved=daily.health(self.config,AT+timedelta(minutes=2))
        self.assertNotIn('mercor:undercoverage',resolved['active'])
        self.assertTrue(any(event['key']=='mercor:undercoverage' and event['kind']=='recovered' for event in resolved['events']))


class RepairIntegrationTests(unittest.TestCase):
    setUp=coverage.CoverageIntegrationTests.setUp

    def test_real_fresh_repair_collects_only_selected_source_and_never_replays_daily_capture(self):
        at=T0+timedelta(days=1,hours=3)
        original=self.root/'state/runs'/daily.slot_at(at).strftime('%Y%m%dT060000Z')/'run.json'
        daily.write_json(original,dict(outcome='failed',sentinel='original consumed daily run'))
        before=original.read_bytes();protected=daily.protected_domains(self.db)
        untouched=self.root/'state/rws-state.json';daily.write_json(untouched,dict(sentinel='unselected'))
        old=untouched.read_bytes();transport=coverage.Transport()
        with coverage.offline(at,transport):
            receipt=daily.reserve_repair_run(self.config,at,'fixture-explicit-repair',['appen'])
            for phase in ('prepare','collect-appen','backup','publish-appen','finish'):
                daily.collect_phase(self.config,receipt['run_id'],phase)
            daily.finish_run_sources(self.config,receipt)
            self.assertIsNone(daily.reserve_repair_run(self.config,at,'fixture-explicit-repair',['appen']))
        self.assertEqual({source for source,_,_ in transport.calls},{'appen'})
        row=receipt['sources']['appen']
        self.assertTrue(row['qualifying_observation'])
        self.assertEqual(row['publication_requests_used'],0)
        self.assertEqual(daily.parse(row['last_qualifying_verification']),at)
        self.assertEqual(original.read_bytes(),before)
        self.assertEqual(untouched.read_bytes(),old)
        self.assertEqual(daily.protected_domains(self.db),protected)


if __name__=='__main__':unittest.main()

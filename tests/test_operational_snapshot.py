"""Historical incident chronology, coherent current state and isolated delivery."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from pathlib import Path
import tempfile,unittest,subprocess
from unittest.mock import Mock,patch
from scripts import daily_inventory as cli
from wahojobs import daily_inventory as d,operational_email as email
from wahojobs.maintenance_gate import operation_gate

AT=datetime(2026,9,25,14,11,tzinfo=timezone.utc)
def event(number,kind,at,key='application:unavailable'):
    return dict(id=f'{number:032x}',kind=kind,key=key,at=at.isoformat(),delivery='pending')
def operating(state='suspended',available=True):
    return dict(coherent=True,started_at=AT.isoformat(),completed_at=AT.isoformat(),
        application=dict(available=available,checked_at=AT.isoformat()),
        collection=dict(state=state,checked_at=AT.isoformat(),next_execution='Sat 2026-09-26 06:00:00 UTC'))

class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.config=dict(state_directory=str(self.root),first_run_at='2026-09-23T06:00:00+00:00',enabled=True)
        self.events=[event(1,'opened',AT-timedelta(minutes=59)),event(2,'recovered',AT-timedelta(minutes=50))]
        self.context=dict(checked_at=AT.isoformat(),operating=operating(),application_ready=True,publication_paused=True,
            cycle=dict(state='failed',outcome='recovery_failed',scheduled_at='2026-09-25T06:00:00+00:00'),expired_records=49)
    def test_historical_failure_and_batched_resolution_are_not_current_outage(self):
        result=email.message(list(reversed(self.events)),self.context)
        self.assertIn('incident resolved',result['subject']);self.assertNotIn('CRITICAL',result['subject'])
        self.assertIn('Application: available',result['text']);self.assertIn('Last cycle (25 Sep 2026 06:00 UTC): Daily check failed',result['text'])
        self.assertIn('Historical application outage',result['text']);self.assertNotIn('CRITICAL: beta application unavailable',result['text'])
        self.assertIn('Collection: suspended',result['text']);self.assertIn('49 exact posting records',result['text'])
        for e in self.events:self.assertIn(e['id'],result['text'])
    def test_clearance_before_and_after_generation_does_not_rewrite_snapshot(self):
        paused=deepcopy(self.context);enabled=deepcopy(paused);enabled['operating']=operating('enabled')
        before=email.message(self.events,paused);after=email.message(self.events,enabled)
        self.assertIn('Collection: suspended',before['text']);self.assertIn('Collection: enabled',after['text'])
        self.assertEqual(email.message(self.events,paused),before)
    def test_changed_gate_during_readiness_is_unknown_with_observation_times(self):
        first=operating()['collection'];second=operating('enabled')['collection']
        with patch.object(cli,'collection_observation',side_effect=[first,second]),patch.object(cli,'application_ready',return_value=True):
            snapshot=cli.operating_snapshot(self.config)
        self.assertFalse(snapshot['coherent']);self.assertEqual(snapshot['collection']['state'],'unknown')
        self.assertTrue(snapshot['application']['available'])
    def test_collection_uses_actual_timer_and_effective_conditions(self):
        timer='ActiveState=active\nUnitFileState=enabled\nNextElapseUSecRealtime=Sat 2026-09-26 06:00:00 UTC\n'
        with patch.object(cli.subprocess,'check_output',side_effect=[timer,'[Unit]\n','no\n']):
            self.assertEqual(cli.collection_observation(self.config)['state'],'enabled')
        with patch.object(cli.subprocess,'check_output',side_effect=[timer,'[Unit]\nConditionPathExists=/missing-fixture-20260925\n','no\n']):
            self.assertEqual(cli.collection_observation(self.config)['state'],'blocked')
        with patch.object(cli.subprocess,'check_output',side_effect=[timer.replace('active','inactive'),'[Unit]\n','no\n']):
            self.assertEqual(cli.collection_observation(self.config)['state'],'suspended')
        with patch.object(cli.subprocess,'check_output',side_effect=[timer,'[Unit]\n','yes\n']):
            self.assertEqual(cli.collection_observation(self.config)['state'],'blocked')
    def test_urgent_outage_survives_broken_source_scan_and_deduplicates(self):
        prior=dict(active={'run:failed':dict(reason='recovery_failed')},events=[],context=self.context)
        d.write_json(self.root/'health.json',prior)
        with patch.object(d,'health_issues',side_effect=AssertionError('source scan forbidden')):
            failed=d.health(self.config,AT,application_ready=False,operating=operating('suspended',False),urgent=True)
            again=d.health(self.config,AT+timedelta(seconds=2),application_ready=False,operating=operating('suspended',False),urgent=True)
            ready=d.health(self.config,AT+timedelta(minutes=1),application_ready=True,operating=operating('suspended'),urgent=True)
        self.assertIn('run:failed',ready['active'])
        self.assertEqual(sum(e['kind']=='opened' and e['key']=='application:unavailable' for e in again['events']),1)
        self.assertEqual(sum(e['kind']=='recovered' and e['key']=='application:unavailable' for e in ready['events']),1)
        self.assertTrue(failed['context']['operating']['application']['available'] is False)
    def test_clearance_emits_once_and_unknown_readiness_never_recovers(self):
        failed=d.health(self.config,AT,application_ready=False,operating=operating(),urgent=True)
        unknown=d.health(self.config,AT,application_ready=None,operating=operating('enabled',None),urgent=True)
        repeated=d.health(self.config,AT,application_ready=None,operating=operating('enabled',None),urgent=True)
        self.assertIn('application:unavailable',unknown['active'])
        transitions=[e for e in repeated['events'] if e['key']=='collection:state']
        self.assertEqual([e['issue']['current_state'] for e in transitions],['suspended','enabled'])
        self.assertFalse(any(e['kind']=='recovered' for e in repeated['events']))
    def test_timer_disable_failure_still_queues_nonblocking_urgent_service(self):
        config=dict(self.config,database=d.DATABASE)
        transport=Mock(side_effect=[subprocess.CalledProcessError(1,'systemctl'),Mock()])
        with patch.object(cli.os,'name','posix'),patch.object(cli.os,'geteuid',return_value=0,create=True),patch.object(d,'write_json'),patch.object(cli.subprocess,'run',transport):
            with self.assertRaises(subprocess.CalledProcessError):cli.suspend_publication(config,{'run_id':'fixture'})
        self.assertEqual(transport.call_args.args[0],['systemctl','start','--no-block','wahojobs-inventory-urgent.service'])
    def test_delivery_retains_rendered_snapshot_attempt_acceptance_and_no_db_gate(self):
        packet=dict(version=2,application='wahojobs-beta',recipient=email.ALERT_RECIPIENT,events=self.events,context=self.context)
        database=self.root/'product.sqlite3'
        def transport(*args):
            with operation_gate(database):pass
            batches=list((self.root/'delivery-batches').glob('*.json'));self.assertEqual(len(batches),1)
            saved=d.read_json(batches[0]);self.assertEqual(saved['context'],self.context)
            ledger=d.read_json(self.root/'resend-delivery.json')
            self.assertEqual(ledger['events'][self.events[0]['id']]['status'],'attempted')
            return 'fixture-request'
        sender=Mock(side_effect=transport)
        result=email.send_packet(packet,'re_isolated_test_credential',self.root,transport=sender,at=AT)
        self.assertEqual(result,'accepted_by_api')
        row=d.read_json(self.root/'resend-delivery.json')['events'][self.events[0]['id']]
        self.assertEqual(row['snapshot_checked_at'],AT.isoformat());self.assertIn('accepted_at',row)
        email.send_packet(packet,'re_isolated_test_credential',self.root,transport=sender,at=AT+timedelta(days=2))
        self.assertEqual(sender.call_count,1)
    def test_specific_coverage_resolution_keeps_global_cycle_failed(self):
        result=email.message([event(3,'recovered',AT,'dataforce:coverage')],self.context)
        self.assertIn('Daily check failed',result['text']);self.assertIn('DataForce: the coverage problem resolved',result['text'])
    def test_failure_is_retained_if_recovered_before_urgent_observation(self):
        with operation_gate(self.root/'health'):
            with patch.object(d,'now',return_value=AT-timedelta(minutes=2)):
                signal=d.record_availability_failure(self.config,reason='actual_failed_startup',unit=d.SERVICE)
            # The actual failure is durable while normal health owns its gate.
            self.assertTrue((self.root/'availability-signals'/(signal['id']+'.json')).exists())
        result=d.health(self.config,AT,application_ready=True,operating=operating(),urgent=True)
        again=d.health(self.config,AT,application_ready=True,operating=operating(),urgent=True)
        events=[e for e in again['events'] if e['key']=='application:unavailable']
        self.assertEqual([e['kind'] for e in events],['opened','recovered'])
        self.assertEqual(events[0]['at'],signal['observed_at']);self.assertEqual(events[1]['at'],AT.isoformat())
        self.assertNotIn('application:unavailable',result['active'])
    def test_urgent_scan_does_not_hide_intervening_source_recovery(self):
        d.write_json(self.root/'health.json',dict(active={'mercor:coverage':dict(reason='failed')},events=[],
            checked_at=(AT-timedelta(hours=1)).isoformat(),context=dict(source_state_checked_at=(AT-timedelta(hours=1)).isoformat())))
        d.write_json(self.root/'mercor-state.json',dict(qualifying_observation=True,ended_at=(AT-timedelta(minutes=30)).isoformat()))
        d.health(self.config,AT,application_ready=True,operating=operating(),urgent=True)
        with patch.object(d,'health_issues',return_value={}):
            result=d.health(self.config,AT+timedelta(minutes=1),application_ready=True,operating=operating())
        self.assertTrue(any(e['key']=='mercor:coverage' and e['kind']=='recovered' for e in result['events']))
    def test_source_headline_cannot_hide_current_unavailability(self):
        value=event(5,'status_changed',AT,'micro1:coverage');value['issue']={'reason':'disabled_after_http_403'}
        context=dict(self.context,operating=operating('suspended',False))
        self.assertIn('CRITICAL',email.message([value],context)['subject'])
    def test_failure_after_readiness_remains_pending_until_a_later_check(self):
        with patch.object(d,'now',return_value=AT+timedelta(seconds=1)):
            signal=d.record_availability_failure(self.config,reason='new_failure_after_readiness',unit=d.SERVICE)
        first=d.health(self.config,AT+timedelta(seconds=2),application_ready=True,operating=operating(),urgent=True)
        self.assertNotIn(signal['id'],first['processed_availability_signals'])
        self.assertFalse(any(e['kind']=='recovered' for e in first['events']))
        self.assertTrue((self.root/'urgent-health-pending.json').exists())
        later=operating();later['application']['checked_at']=(AT+timedelta(seconds=3)).isoformat()
        second=d.health(self.config,AT+timedelta(seconds=3),application_ready=True,operating=later,urgent=True)
        self.assertEqual([e['kind'] for e in second['events'] if e['key']=='application:unavailable'],['opened','recovered'])
    def test_signal_persistence_failure_cannot_skip_publication_hold(self):
        config=dict(self.config,database=str(self.root/'product.sqlite3'))
        with patch.object(d,'record_availability_failure',side_effect=OSError('signal folder fixture failure')):
            with self.assertRaises(OSError):cli.suspend_publication(config,{'run_id':'fixture'})
        self.assertTrue((self.root/'publication-hold.json').exists())
        with self.assertRaisesRegex(ValueError,'operator_clearance'):cli.supervise(config,'fixture','timer',operations=Mock())

if __name__=='__main__':unittest.main()

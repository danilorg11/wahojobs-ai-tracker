"""Recovery allocation controls; no systemd or authoritative storage access."""
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import daily_inventory as cli
from wahojobs import daily_inventory as daily
from wahojobs.maintenance_gate import operation_gate
from tests.evidence_maintenance_support import T0


class RecoveryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.operations=cli.NativeOperations({'database':'synthetic.sqlite'},'synthetic-policy')

    def test_only_complete_stopped_state_skips_readiness(self):
        cases=[('inactive','0','0',True),('failed','0','0',True),
            ('active','1','0',False),('activating','0','1',False),
            ('deactivating','0','0',False),('inactive','1','0',False),
            ('failed','0','1',False),('unknown','0','0',False)]
        for state,main,control,expected in cases:
            with self.subTest(state=state,main=main,control=control),patch.object(
                    cli.subprocess,'check_output',return_value=f'ActiveState={state}\nMainPID={main}\nControlPID={control}\n') as query:
                self.assertIs(self.operations.application_stopped(3),expected)
                self.assertEqual(query.call_args.kwargs['timeout'],3)
        for raw in ('ActiveState=inactive\nMainPID=0\n','bad state',''):
            with patch.object(cli.subprocess,'check_output',return_value=raw):
                self.assertFalse(self.operations.application_stopped(10))

    def test_state_query_failure_preserves_readiness_path(self):
        for error in (OSError('synthetic'),subprocess.TimeoutExpired('show',5)):
            with patch.object(cli.subprocess,'check_output',side_effect=error) as query:
                self.assertFalse(self.operations.application_stopped(90))
                self.assertEqual(query.call_args.kwargs['timeout'],5)
        with patch.object(cli.subprocess,'check_output') as query:
            with self.assertRaises(TimeoutError):self.operations.application_stopped(0)
            query.assert_not_called()

    def test_no_dispatch_without_positive_repair_allowance(self):
        for remaining in (-1,0,20,80):
            with self.subTest(remaining=remaining),patch.object(cli,'bounded_process') as process:
                with self.assertRaisesRegex(TimeoutError,'repair_allowance'):
                    self.operations.restore(remaining)
                process.assert_not_called()

    def test_start_and_final_health_never_dispatch_after_deadline(self):
        for clock,calls in (([0,420],1),([0,10,420],2)):
            with patch.object(cli.time,'monotonic',side_effect=clock),patch.object(cli,'bounded_process') as process:
                with self.assertRaisesRegex(TimeoutError,'recovery_deadline'):
                    self.operations.restore(420)
                self.assertEqual(process.call_count,calls)

    def test_repair_timeout_failure_and_cancellation_never_start_service(self):
        errors=(subprocess.TimeoutExpired('repair-storage',40),
            RuntimeError('bounded_process_failed'),OSError('cannot_spawn'),
            InterruptedError('cancelled'))
        for error in errors:
            with self.subTest(error=type(error).__name__),patch.object(
                    cli,'bounded_process',side_effect=error) as process:
                with self.assertRaises(type(error)) as raised:
                    self.operations.restore(420)
                self.assertIs(raised.exception,error)
                process.assert_called_once()
                self.assertIn('repair-storage',process.call_args.args[0])
                self.assertEqual(process.call_args.kwargs['timeout'],160)

    @unittest.skipUnless(os.name=='posix','Native process-group teardown requires POSIX')
    def test_real_repair_subprocess_timeout_and_nonzero_exit_block_start(self):
        native_process=cli.bounded_process
        for code,error in (('raise SystemExit(7)',RuntimeError),
                ('import time; time.sleep(30)',subprocess.TimeoutExpired)):
            with self.subTest(error=error.__name__),tempfile.TemporaryDirectory(prefix='repair-child-') as temp:
                calls=[]
                def isolated_repair(args,**kwargs):
                    calls.append(args)
                    self.assertIn('repair-storage',args)
                    return native_process([sys.executable,'-B','-c',code],cwd=temp,timeout=.1)
                with patch.object(cli,'bounded_process',side_effect=isolated_repair):
                    with self.assertRaises(error):self.operations.restore(420)
                self.assertEqual(len(calls),1)

    def test_stopped_observation_does_not_bypass_live_process_recheck(self):
        # A start may race with the observation. The actual repair entry keeps
        # its independent PID guard; the observation grants no storage access.
        with patch.object(cli,'verify_release_configuration'),patch.object(
                cli,'verify_recovery_parent'),patch.object(cli.subprocess,
                'check_output',return_value='MainPID=123\nControlPID=0\n'):
            with self.assertRaisesRegex(ValueError,'application_still_active'):
                cli.repair_storage({'database':'synthetic.sqlite'})

    def test_readiness_preserves_retry_cap_and_respects_remaining_deadline(self):
        for remaining,expected in ((100,25),(4,4)):
            with patch.object(cli,'bounded_process') as process:
                self.operations.ready(remaining)
                self.assertEqual(process.call_args.kwargs['timeout'],expected)
        with patch.object(cli,'bounded_process') as process:
            with self.assertRaises(TimeoutError):self.operations.ready(0)
            process.assert_not_called()

    def run_old_recovery(self,stopped,*,ready_succeeds=False,probe_seconds=2):
        with tempfile.TemporaryDirectory(prefix='recovery-budget-') as temp:
            root=Path(temp);config={'database':str(root/'fixture.sqlite'),'state_directory':temp}
            with operation_gate(config['database']):pass
            receipt=daily.reserve_run(root,T0.replace(hour=6),T0.replace(hour=6),'timer')
            receipt.update(maintenance_started_at=daily.stamp(T0),normal_service_resumed=False,outcome='failed')
            path=root/'runs'/receipt['run_id']/'run.json';daily.write_json(path,receipt)
            original=path.read_bytes();clock=[0.0];timeouts=[]
            operations=cli.NativeOperations(config,'fixture')
            def preflight():clock[0]+=3
            def state_probe(remaining):
                self.assertEqual(remaining,407)
                clock[0]+=probe_seconds
                return stopped
            def process(args,**kwargs):
                timeouts.append(kwargs['timeout'])
                if 'repair-storage' in args:clock[0]+=4
                elif 'start' in args:clock[0]+=60
                elif ready_succeeds:clock[0]+=1
                elif len(timeouts)==1 and stopped is not True:
                    clock[0]+=kwargs['timeout']
                    raise TimeoutError('not_ready')
            with patch.object(cli,'NativeOperations',return_value=operations),patch.object(
                    operations,'recovery_preflight',side_effect=preflight),patch.object(
                    operations,'application_stopped',side_effect=state_probe),patch.object(
                    cli.time,'monotonic',side_effect=lambda:clock[0]),patch.object(
                    daily,'now',return_value=T0+timedelta(hours=2)),patch.object(
                    cli,'bounded_process',side_effect=process),patch.object(cli,'suspend_publication') as suspend:
                cli.recover(config,'fixture')
                suspend.assert_not_called()
            self.assertEqual(path.read_bytes(),original)
            self.assertTrue(daily.read_json(path.parent/'application-recovery.json')['application_ready'])
            return timeouts,clock[0]

    def test_stopped_old_receipt_retains_shared_budget_without_polling(self):
        timeouts,elapsed=self.run_old_recovery(True)
        self.assertEqual(timeouts,[145,245,20])
        self.assertEqual(elapsed,69)

    def test_uncertain_live_service_keeps_existing_readiness_retries(self):
        timeouts,elapsed=self.run_old_recovery(False,ready_succeeds=True)
        self.assertEqual(timeouts,[25])
        self.assertEqual(elapsed,6)

    def test_failed_preliminary_readiness_still_consumes_shared_budget(self):
        timeouts,elapsed=self.run_old_recovery(False,probe_seconds=0)
        self.assertEqual(timeouts,[25,122,245,20])
        self.assertEqual(elapsed,92)

    def test_stopped_repair_failure_preserves_receipt_and_requires_recovery(self):
        for error in (subprocess.TimeoutExpired('repair-storage',30),RuntimeError('repair_failed')):
            with self.subTest(error=type(error).__name__),tempfile.TemporaryDirectory(prefix='stopped-repair-') as temp:
                root=Path(temp);config={'database':str(root/'fixture.sqlite'),'state_directory':temp}
                with operation_gate(config['database']):pass
                at=T0.replace(hour=6)
                receipt=daily.reserve_run(root,at,at,'timer')
                receipt.update(maintenance_started_at=daily.stamp(at),normal_service_resumed=False,outcome='failed')
                path=root/'runs'/receipt['run_id']/'run.json';daily.write_json(path,receipt)
                original=path.read_bytes();operations=cli.NativeOperations(config,'fixture')
                with patch.object(cli,'NativeOperations',return_value=operations),patch.object(
                        operations,'recovery_preflight'),patch.object(cli.subprocess,'check_output',
                        return_value='ActiveState=failed\nMainPID=0\nControlPID=0\n') as query,patch.object(
                        cli.time,'monotonic',return_value=0),patch.object(
                        daily,'now',return_value=at+timedelta(hours=2)),patch.object(
                        cli,'bounded_process',side_effect=error) as process,patch.object(cli,'suspend_publication') as suspend:
                    with self.assertRaises(type(error)) as raised:cli.recover(config,'fixture')
                    self.assertIs(raised.exception,error)
                    query.assert_called_once()
                    process.assert_called_once()
                    self.assertIn('repair-storage',process.call_args.args[0])
                    self.assertEqual(process.call_args.kwargs['timeout'],150)
                    suspend.assert_called_once_with(config,receipt)
                self.assertEqual(path.read_bytes(),original)
                self.assertFalse((path.parent/'application-recovery.json').exists())
                self.assertEqual(cli.pending_recovery(root),[path])

    def test_state_probe_can_exhaust_deadline_without_dispatching_any_phase(self):
        with tempfile.TemporaryDirectory(prefix='recovery-probe-') as temp:
            root=Path(temp);config={'database':str(root/'fixture.sqlite'),'state_directory':temp}
            with operation_gate(config['database']):pass
            at=T0.replace(hour=6)
            receipt=daily.reserve_run(root,at,at,'timer')
            receipt.update(maintenance_started_at=daily.stamp(at),normal_service_resumed=False,outcome='failed')
            path=root/'runs'/receipt['run_id']/'run.json';daily.write_json(path,receipt)
            clock=[0];operations=cli.NativeOperations(config,'fixture')
            def preflight():clock[0]=408
            def query(*args,**kwargs):
                self.assertEqual(kwargs['timeout'],2)
                clock[0]=410
                raise subprocess.TimeoutExpired('show',2)
            with patch.object(cli,'NativeOperations',return_value=operations),patch.object(
                    operations,'recovery_preflight',side_effect=preflight),patch.object(
                    cli.subprocess,'check_output',side_effect=query),patch.object(
                    cli.time,'monotonic',side_effect=lambda:clock[0]),patch.object(
                    daily,'now',return_value=at+timedelta(hours=2)),patch.object(
                    cli,'bounded_process') as process,patch.object(cli,'suspend_publication') as suspend:
                with self.assertRaisesRegex(TimeoutError,'recovery_deadline'):cli.recover(config,'fixture')
                process.assert_not_called()
                suspend.assert_called_once()
            self.assertFalse((path.parent/'application-recovery.json').exists())


if __name__=='__main__':unittest.main()

"""Offline native phase wiring for scheduled and explicit v2-backed publication."""
from contextlib import redirect_stdout
from datetime import timedelta
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from scripts import daily_inventory as cli
from wahojobs import beta_recovery as recovery, daily_inventory as daily, operational_budgets as budgets
from wahojobs.recovery_archive import VERSION
from tests import test_daily_source_coverage as coverage
from tests.evidence_maintenance_support import T0


class DailyPackagedBackupIntegrationTests(unittest.TestCase):
    setUp = coverage.CoverageIntegrationTests.setUp

    def exercise(self, *, repair=False):
        at=T0.replace(hour=6)+timedelta(days=1)
        for source in self.config['sources']:
            self.config['sources'][source]['enabled']=source=='appen'
        daily.validate_sources(self.config['sources'])
        protected=daily.protected_domains(self.db)
        transport=coverage.Transport();calls=[];returned=[];verified=[];prepared_proofs=[]
        original_create=recovery.create_snapshot
        original_verify=recovery.verify_snapshot
        prior_umask=os.umask(0o077);self.addCleanup(os.umask,prior_umask)

        class OfflineNative(cli.NativeOperations):
            def preflight(self):calls.append('preflight')
            def ready(self):calls.append('ready')
            def stop(self,remaining):
                calls.append('stop')
                self.assert_bounds(remaining)
            def restore(self,remaining):
                calls.append('restore')
                if remaining!=daily.RECOVERY_SECONDS:raise AssertionError('recovery allowance changed')
                # Standalone verification retains the deep recovery path.
                snapshots=list((Path(self.config['state_directory'])/'backups').glob('*/COMPLETE.sha256'))
                if len(snapshots)!=1:raise AssertionError('one completed snapshot required')
                original_verify(snapshots[0].parent)
            @staticmethod
            def assert_bounds(remaining):
                if not 0<remaining<=daily.PUBLICATION_SECONDS:raise AssertionError('publication allowance changed')

        native=OfflineNative(self.config,'isolated-policy')
        def worker_claim(config,run_id,phase):
            remaining=cli._claim_worker_deadline(config,run_id,phase,os.getpid(),time.monotonic())
            return time.monotonic()+max(.01,remaining-min(3,remaining/4))
        def worker_process(arguments,*,timeout,user,capture_output=False,**unused):
            self.assertEqual(user,'wahojobs-beta');self.assertGreater(timeout,0)
            self.assertLessEqual(timeout,daily.execution_seconds(self.config))
            arguments=arguments[arguments.index('worker'):]
            phase=arguments[arguments.index('--phase')+1];calls.append(phase)
            if phase=='backup':self.assertLessEqual(timeout,budgets.BACKUP_SECONDS)
            output=io.StringIO()
            with redirect_stdout(output):self.assertEqual(cli.main(arguments),0)
            if capture_output:
                proof=json.loads(output.getvalue());prepared_proofs.append(proof)
                self.assertEqual(proof['snapshot_version'],VERSION)
                prepared=Path(self.config['state_directory'])/'backups'/(proof['run_id']+'.prepared')
                self.assertEqual(proof['prepared_sha256'],sha256((prepared/'PREPARED.json').read_bytes()).hexdigest())
                return output.getvalue()
            self.assertEqual(output.getvalue(),'')
        def create(*args,**kwargs):
            result=original_create(*args,**kwargs);returned.append(result)
            self.assertEqual(result['version'],VERSION)
            self.assertEqual(kwargs['expected_preparation_sha256'],prepared_proofs[-1]['prepared_sha256'])
            return result
        def verify(*args,**kwargs):
            self.assertIs(kwargs['trusted_manifest'],returned[-1])
            verified.append(kwargs['trusted_manifest'])
            return original_verify(*args,**kwargs)

        original_daily=None
        if repair:
            path=Path(self.config['state_directory'])/'runs'/daily.slot_at(at).strftime('%Y%m%dT060000Z')/'run.json'
            daily.write_json(path,dict(outcome='failed',sentinel='consumed scheduled receipt'))
            original_daily=(path,path.read_bytes())
            at+=timedelta(hours=1)
        with coverage.offline(at,transport),patch.object(cli,'private_policy',return_value=self.config),\
                patch.object(daily,'validate_policy'),patch.object(cli,'claim_worker',side_effect=worker_claim),\
                patch.object(cli,'bounded_process',side_effect=worker_process),\
                patch.object(recovery,'create_snapshot',side_effect=create),\
                patch.object(recovery,'verify_snapshot',side_effect=verify):
            receipt=cli.supervise(self.config,'isolated-policy','manual' if repair else 'timer',operations=native,
                **(dict(repair_request='packaged-fixture-repair',repair_sources=['appen']) if repair else {}))
        self.assertEqual(receipt['outcome'],'complete_with_coverage_gaps',receipt)
        self.assertTrue(receipt['normal_service_resumed'])
        self.assertEqual(len(returned),1);self.assertEqual(len(verified),1)
        self.assertEqual(calls.count('restore'),1)
        self.assertLess(calls.index('prepare-backup'),calls.index('stop'))
        self.assertEqual({source for source,_,_ in transport.calls},{'appen'})
        self.assertEqual(receipt['sources']['appen']['publication_requests_used'],0)
        self.assertEqual(daily.parse(receipt['sources']['appen']['last_qualifying_verification']),at)
        self.assertEqual(daily.protected_domains(self.db),protected)
        target=Path(self.config['state_directory'])/'runs'/receipt['run_id']
        self.assertEqual(daily.read_json(target/'backup.json')['snapshot_version'],VERSION)
        self.assertEqual(daily.read_json(target/'journal-preparation.json')['snapshot_version'],VERSION)
        self.assertEqual((daily.PUBLICATION_SECONDS,daily.RECOVERY_SECONDS),(600,420))
        if original_daily:self.assertEqual(original_daily[0].read_bytes(),original_daily[1])

    def test_scheduled_run_publishes_with_v2_preparation_and_direct_manifest_proof(self):
        self.exercise()

    def test_manual_repair_uses_same_v2_worker_ipc_and_preserves_scheduled_receipt(self):
        self.exercise(repair=True)


if __name__=='__main__':unittest.main()

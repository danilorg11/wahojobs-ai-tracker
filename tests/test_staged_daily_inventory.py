"""Offline online-collection/publication boundary and recovery regression tests."""
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import daily_inventory as cli
from wahojobs import daily_inventory as daily, evidence_maintenance as maintenance
from wahojobs.crawler import pipeline, staged_observation as staged
from wahojobs.database_lifetime_ownership import (
    acquire_database_lifetime_ownership, release_database_lifetime_ownership, ROLE_DURABLE_RUNTIME)
from tests import test_daily_source_coverage as coverage
from tests.test_daily_source_coverage import Transport, offline
from tests.evidence_maintenance_support import T0


class StagedIntegrationTests(unittest.TestCase):
    setUp = coverage.CoverageIntegrationTests.setUp

    def prepare(self, name='staged'):
        at=T0+timedelta(days=1)
        with offline(at,Transport()):daily.collect_phase(self.config,name,'prepare')
        return at,self.root/'state/runs'/name

    def test_collection_keeps_runtime_ownership_and_preserves_intervening_user_writes(self):
        at,target=self.prepare()
        before=self.db.read_bytes()
        lease=acquire_database_lifetime_ownership(self.db,role=ROLE_DURABLE_RUNTIME)
        try:
            with offline(at,Transport()):daily.collect_phase(self.config,'staged','collect-appen')
            self.assertEqual(self.db.read_bytes(),before)
            # Session activity while collection is online must remain present;
            # publication must use current storage, never an old database copy.
            with closing(sqlite3.connect(self.db)) as db:
                seen=daily.parse(db.execute('SELECT last_seen_at FROM account_sessions LIMIT 1').fetchone()[0])+timedelta(seconds=1)
                db.execute('UPDATE account_sessions SET last_seen_at=?',(seen.isoformat(),));db.commit()
        finally:release_database_lifetime_ownership(lease,role=ROLE_DURABLE_RUNTIME,database_path=self.db)
        current=daily.protected_domains(self.db)
        later=at+timedelta(minutes=20)
        with offline(later,Transport()),patch.dict(pipeline.CRAWLERS,appen=Mock(side_effect=AssertionError('Publication cannot collect'))):
            for phase in ('backup','publish-appen','finish'):daily.collect_phase(self.config,'staged',phase)
        self.assertEqual(current,daily.protected_domains(self.db))
        row=daily.read_json(target/'appen.json')
        self.assertTrue(row['qualifying_observation']);self.assertEqual(row['publication_requests_used'],0)
        self.assertEqual(daily.parse(row['last_qualifying_verification']),at)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT last_seen_at FROM account_sessions LIMIT 1').fetchone()[0],seen.isoformat())
        manifest=json.loads((self.root/'state/backups/staged/manifest.json').read_text())
        collection=daily.read_json(target/'appen-collection.json')['plan_id']
        self.assertTrue(any('journal/'+collection+'/' in str(entry) for entry in manifest['files']))

    def test_post_commit_summary_failure_recovers_qualifying_evidence_without_network(self):
        at,target=self.prepare('reporting')
        with offline(at,Transport()):
            daily.collect_phase(self.config,'reporting','collect-appen')
            daily.collect_phase(self.config,'reporting','backup')
            with patch.object(daily,'save_source',side_effect=OSError('fixture summary persistence failure')):
                with self.assertRaises(OSError):daily.collect_phase(self.config,'reporting','publish-appen')
        receipt=dict(run_id='reporting',trigger='timer',started_at=daily.stamp(at),maintenance_seconds=2)
        with offline(at+timedelta(hours=3),Transport()):daily.finish_run_sources(self.config,receipt)
        row=receipt['sources']['appen']
        self.assertTrue(row['qualifying_observation'],row);self.assertEqual(row['requests_used'],1)
        self.assertEqual(daily.parse(row['last_qualifying_verification']),at)

    def test_binding_expiry_tamper_and_duplicate_publication_are_rejected(self):
        at,target=self.prepare('bindings')
        with offline(at,Transport()):
            daily.collect_phase(self.config,'bindings','collect-mercor')
            args=dict(run_id='bindings',code_commit=self.config['code_commit'],journal_root=self.journal)
            for changes in (dict(run_id='other'),dict(code_commit='b'*40)):
                with self.assertRaises(ValueError):staged.load(target,'mercor',**dict(args,**changes))
            staged.load(target,'mercor',**args,consume=True)
            with self.assertRaises(FileExistsError):staged.load(target,'mercor',**args,consume=True)
        with offline(at+timedelta(hours=2),Transport()),self.assertRaisesRegex(ValueError,'not_admissible'):
            staged.load(target,'mercor',**args)
        plan=daily.read_json(target/'mercor-collection.json')['plan_id']
        raw=next((self.journal/plan).glob('*.raw'));raw.write_bytes(b'tampered fixture')
        with offline(at,Transport()),self.assertRaisesRegex(ValueError,'integrity'):
            staged.load(target,'mercor',**args)

    def test_reduced_policy_cannot_dispatch_from_old_coverage_plan(self):
        at,_=self.prepare('disabled');self.config['sources']['appen']['enabled']=False
        transport=Transport()
        with offline(at,transport),self.assertRaisesRegex(ValueError,'not_due'):
            daily.collect_phase(self.config,'disabled','collect-appen')
        self.assertEqual(transport.calls,[])


class StagedSupervisorTests(unittest.TestCase):
    def test_online_failure_never_stops_or_restores_beta(self):
        with tempfile.TemporaryDirectory() as temp:
            config=dict(state_directory=temp,database=str(Path(temp)/'fixture.sqlite3'),first_run_at=daily.stamp(T0.replace(hour=6)))
            operations=Mock();operations.collect.side_effect=TimeoutError()
            with patch.object(daily,'now',return_value=T0.replace(hour=6)):
                receipt=cli.supervise(config,'fixture','timer',operations=operations)
            operations.stop.assert_not_called();operations.publish.assert_not_called();operations.restore.assert_not_called()
            self.assertEqual(receipt['maintenance_seconds'],0);self.assertNotIn('maintenance_started_at',receipt)
            self.assertTrue(receipt['normal_service_resumed'])

    def test_publication_timeout_reserves_time_for_later_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'runs/fixture';target.mkdir(parents=True)
            daily.write_json(target/'publication-sources.json',['appen','mercor','rws'])
            daily.write_json(target/'publication-weights.json',dict(appen=5624,mercor=411,rws=1))
            native=cli.NativeOperations(dict(state_directory=temp),'fixture');clock=[0];calls=[]
            def phase(run_id,name,deadline):
                self.assertGreater(deadline,clock[0]);calls.append(name)
                if name=='publish-appen':self.assertGreater(deadline-clock[0],77)
                if name=='publish-appen':clock[0]=deadline;raise TimeoutError()
                clock[0]+=1
            with patch.object(cli.time,'monotonic',side_effect=lambda:clock[0]),patch.object(native,'phase',side_effect=phase):
                with self.assertRaises(TimeoutError):native.publish('fixture',240)
            self.assertEqual(calls,['backup','publish-appen'])
            self.assertLess(clock[0],240)

    def test_restart_closes_online_only_receipt_without_service_or_collection_actions(self):
        from wahojobs.maintenance_gate import operation_gate
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);config=dict(state_directory=temp,database=str(root/'fixture.sqlite3'))
            with operation_gate(config['database']):pass
            receipt=daily.reserve_run(root,T0.replace(hour=6),T0.replace(hour=6),'timer')
            receipt.update(outcome='running',normal_service_resumed=True)
            path=root/'runs'/receipt['run_id']/'run.json';daily.write_json(path,receipt)
            with patch.object(cli,'NativeOperations') as native,patch.object(daily,'finish_run_sources') as finish:
                cli.recover(config,'fixture')
            finish.assert_called_once();native.return_value.stop.assert_not_called()
            native.return_value.restore.assert_not_called();native.return_value.collect.assert_not_called()
            saved=daily.read_json(path);self.assertEqual(saved['outcome'],'interrupted');self.assertEqual(saved['maintenance_seconds'],0)
            self.assertIsNone(daily.reserve_run(root,T0.replace(hour=6),T0.replace(hour=6),'timer'))

    def test_pending_outage_is_restored_before_old_online_reporting_can_fail(self):
        from wahojobs.maintenance_gate import operation_gate
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);config=dict(state_directory=temp,database=str(root/'fixture.sqlite3'))
            with operation_gate(config['database']):pass
            old=daily.reserve_run(root,T0.replace(hour=6),T0.replace(hour=6),'timer')
            old.update(outcome='running',normal_service_resumed=True)
            oldpath=root/'runs'/old['run_id']/'run.json';daily.write_json(oldpath,old)
            at=T0.replace(hour=6)+timedelta(days=1)
            current=daily.reserve_run(root,at,at,'timer')
            current.update(outcome='running',normal_service_resumed=False,maintenance_started_at=daily.stamp(at))
            daily.write_json(root/'runs'/current['run_id']/'run.json',current)
            original=daily.write_json;calls=[]
            def write(path,value):
                if path==oldpath:
                    calls.append('reporting_failed');raise OSError('fixture reporting failure')
                return original(path,value)
            with patch.object(cli,'NativeOperations') as native,patch.object(daily,'now',return_value=at+timedelta(seconds=10)),patch.object(daily,'write_json',side_effect=write),patch.object(daily,'finish_run_sources'):
                native.return_value.restore.side_effect=lambda remaining:calls.append('restored')
                with self.assertRaises(OSError):cli.recover(config,'fixture')
            self.assertEqual(calls,['restored','reporting_failed'])


if __name__=='__main__':unittest.main()

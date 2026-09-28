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
            with offline(at,Transport()):prepared=daily.collect_phase(self.config,'staged','prepare-backup')
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
            for phase in ('backup','publish-appen','finish'):
                daily.collect_phase(self.config,'staged',phase,
                    **({'expected_preparation_sha256':prepared['prepared_sha256']} if phase=='backup' else {}))
        self.assertEqual(current,daily.protected_domains(self.db))
        row=daily.read_json(target/'appen.json')
        self.assertIs(daily.read_json(target/'appen-plan.json')['config']['staged_baseline'],True)
        self.assertTrue(row['qualifying_observation']);self.assertEqual(row['publication_requests_used'],0)
        self.assertEqual(daily.parse(row['last_qualifying_verification']),at)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT last_seen_at FROM account_sessions LIMIT 1').fetchone()[0],seen.isoformat())
        snapshot=self.root/'state/backups/staged'
        manifest=json.loads((snapshot/'manifest.json').read_text())
        from wahojobs.beta_recovery import logical_snapshot_records
        collection=daily.read_json(target/'appen-collection.json')['plan_id']
        self.assertEqual(manifest['version'],'private_beta_cold_snapshot_v2')
        self.assertTrue(any(name.startswith('journal/'+collection+'/') for name,_ in logical_snapshot_records(snapshot,manifest)))

    def test_nonstaged_worker_still_rejects_corrupt_history_before_any_collection(self):
        at,target=self.prepare('direct-history')
        with closing(sqlite3.connect(self.db)) as connection,connection:
            connection.execute("UPDATE job_source_content_captures SET body=body||' corrupted fixture' "
                "WHERE id=(SELECT a.accepted_capture_id FROM job_source_content_acceptances a "
                "JOIN jobs j ON j.id=a.job_id JOIN companies c ON c.id=j.company_id "
                "WHERE c.slug='alignerr' LIMIT 1)")
        before=self.db.read_bytes();transport=Transport()
        with offline(at,transport),self.assertRaises(RuntimeError):
            daily.collect_phase(self.config,'direct-history','alignerr')
        self.assertEqual(transport.calls,[])
        self.assertEqual(self.db.read_bytes(),before)
        self.assertFalse((target/'alignerr-plan.json').exists())

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
    def test_production_policy_budget_accepts_online_preparation_after_fast_collection(self):
        # Exact enabled-source budget of the September 28 operator repair.
        budgets=dict(alignerr=(100,240),appen=(1,60),dataannotation=(20,180),dataforce=(15,180),
            handshake=(40,120),mercor=(201,240),meridial=(2,150),micro1=(50,240),
            mindrift=(70,360),oneforma=(3,210),outlier=(51,60),rws=(1,60),
            surge=(20,180),turing=(3,210),welocalize=(1,90))
        sources={source:dict(enabled=source!='micro1',http_max=requests,seconds_max=seconds)
            for source,(requests,seconds) in budgets.items()}
        daily.validate_sources(sources)
        self.assertEqual(daily.aggregate(sources),dict(http_max=528,execution_seconds=2580))
        with tempfile.TemporaryDirectory() as temp:
            config=dict(state_directory=temp,sources=sources)
            target=Path(temp)/'runs/fixture';target.mkdir(parents=True)
            receipt=dict(run_id='fixture',outcome='running',supervisor_pid=123,
                execution_deadline_monotonic=2580,active_phase=dict(name='prepare-backup',deadline=2340))
            daily.write_json(target/'run.json',receipt)
            # About 165 seconds of online collection has elapsed. The existing
            # 240-second publication allowance remains reserved.
            self.assertGreater(2340-165,daily.EXECUTION_SECONDS)
            self.assertEqual(cli._claim_worker_deadline(config,'fixture','prepare-backup',123,165),2175)
            self.assertTrue((target/'prepare-backup-dispatch.claim').exists())
            with self.assertRaises(FileExistsError):
                cli._claim_worker_deadline(config,'fixture','prepare-backup',123,165)
            self.assertFalse((target/'prepare-backup-failure.json').exists())

    def test_worker_dispatch_preserves_configured_and_hard_limits_and_preclaim_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'runs/fixture';target.mkdir(parents=True)
            receipt=dict(run_id='fixture',outcome='running',supervisor_pid=123,
                execution_deadline_monotonic=3000,active_phase=dict(name='prepare-backup',deadline=2175))
            daily.write_json(target/'run.json',receipt)
            for limit in (2040,2581):
                with self.subTest(limit=limit),self.assertRaises(ValueError):
                    cli.claim_dispatch(temp,'fixture',123,0,'prepare-backup',execution_limit=limit)
                self.assertFalse((target/'prepare-backup-dispatch.claim').exists())
            receipt['active_phase']['deadline']=2581
            daily.write_json(target/'run.json',receipt)
            with self.assertRaises(ValueError):
                cli.claim_dispatch(temp,'fixture',123,0,'prepare-backup',execution_limit=2580)
            receipt['active_phase']['deadline']=100
            daily.write_json(target/'run.json',receipt)
            with self.assertRaisesRegex(ValueError,'worker_execution_deadline_expired'):
                cli._claim_worker_deadline(dict(state_directory=temp),'fixture','prepare-backup',123,100)
            self.assertFalse((target/'prepare-backup-dispatch.claim').exists())
            diagnostic=daily.read_json(target/'prepare-backup-failure.json')
            self.assertEqual(diagnostic['phase'],'prepare-backup')
            self.assertEqual(diagnostic['reason'],'worker_execution_deadline_expired')
            self.assertEqual(diagnostic['run_id'],'fixture')
            with patch.object(daily,'write_json',side_effect=OSError('fixture diagnostic storage failure')),\
                    self.assertRaisesRegex(ValueError,'worker_execution_deadline_expired'):
                cli._claim_worker_deadline(dict(state_directory=temp),'fixture','prepare-backup',123,100)

    def test_wrong_worker_binding_or_duplicate_cannot_write_or_overwrite_phase_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'runs/fixture';target.mkdir(parents=True)
            receipt=dict(run_id='fixture',outcome='running',supervisor_pid=123,
                execution_deadline_monotonic=100,active_phase=dict(name='prepare-backup',deadline=100))
            daily.write_json(target/'run.json',receipt)
            diagnostic=target/'prepare-backup-failure.json'
            daily.write_json(diagnostic,dict(sentinel='retained actual phase failure'))
            before=diagnostic.read_bytes();config=dict(state_directory=temp)
            for run_id,phase,parent in (('fixture','prepare-backup',124),('other','prepare-backup',123),
                    ('fixture','collect-appen',123)):
                with self.subTest(run=run_id,phase=phase,parent=parent),self.assertRaises(ValueError):
                    cli._claim_worker_deadline(config,run_id,phase,parent,1)
                self.assertEqual(diagnostic.read_bytes(),before)
            self.assertFalse((Path(temp)/'runs/other').exists())
            self.assertFalse((target/'collect-appen-failure.json').exists())
            receipt['outcome']='failed';daily.write_json(target/'run.json',receipt)
            with self.assertRaises(ValueError):
                cli._claim_worker_deadline(config,'fixture','prepare-backup',123,1)
            self.assertEqual(diagnostic.read_bytes(),before)
            receipt['outcome']='running';daily.write_json(target/'run.json',receipt)
            self.assertEqual(cli._claim_worker_deadline(config,'fixture','prepare-backup',123,1),99)
            with self.assertRaises(FileExistsError):
                cli._claim_worker_deadline(config,'fixture','prepare-backup',123,1)
            self.assertEqual(diagnostic.read_bytes(),before)
            with self.assertRaisesRegex(ValueError,'worker_execution_deadline_expired'):
                cli._claim_worker_deadline(config,'fixture','prepare-backup',123,100)
            self.assertEqual(diagnostic.read_bytes(),before)

    def test_preparation_digest_crosses_only_the_direct_worker_channel(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'runs/fixture';target.mkdir(parents=True)
            daily.write_json(target/'run.json',dict(prepared_sha256='f'*64))
            daily.write_json(target/'journal-preparation.json',dict(prepared_sha256='f'*64))
            native=cli.NativeOperations(dict(state_directory=temp),'fixture')
            proof=json.dumps(dict(run_id='fixture',prepared_sha256='a'*64,snapshot_version='private_beta_cold_snapshot_v2'))
            with patch.object(cli,'bounded_process',return_value=proof) as process:
                with self.assertRaisesRegex(ValueError,'trusted_journal_preparation_required'):
                    native.phase('fixture','backup',cli.time.monotonic()+60)
                process.assert_not_called()
                native.phase('fixture','prepare-backup',cli.time.monotonic()+60)
                self.assertTrue(process.call_args.kwargs['capture_output'])
                native.phase('fixture','backup',cli.time.monotonic()+60)
                self.assertEqual(process.call_args.args[0][-2:],['--prepared-sha256','a'*64])
            self.assertNotIn('prepared_sha256',daily.read_json(target/'run.json').get('active_phase',{}))
            for invalid in (dict(run_id='other',prepared_sha256='a'*64,snapshot_version='private_beta_cold_snapshot_v2'),
                    dict(run_id='fixture',prepared_sha256='bad',snapshot_version='private_beta_cold_snapshot_v2'),
                    dict(run_id='fixture',prepared_sha256='a'*64,snapshot_version='private_beta_cold_snapshot_v1')):
                with patch.object(cli,'bounded_process',return_value=json.dumps(invalid)),\
                        self.assertRaisesRegex(ValueError,'trusted_journal_preparation_required'):
                    native.phase('fixture','prepare-backup',cli.time.monotonic()+60)

    def test_online_evidence_preparation_reserves_the_existing_publication_window(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'runs/fixture';target.mkdir(parents=True)
            schedule={source:dict(state='due' if source=='appen' else 'disabled',stored_records=1)
                for source in daily.SOURCES}
            daily.write_json(target/'coverage-plan.json',schedule)
            native=cli.NativeOperations(dict(state_directory=temp,sources=daily.default_sources(),
                code_commit='a'*40,journal=str(Path(temp)/'journal')),'fixture')
            clock=[0];calls=[]
            def phase(run_id,name,deadline):
                calls.append((name,deadline));clock[0]+=1
            observation=SimpleNamespace(result=SimpleNamespace(jobs=[1]))
            with patch.object(cli.time,'monotonic',side_effect=lambda:clock[0]),\
                    patch.object(native,'phase',side_effect=phase),patch.object(staged,'load',return_value=(observation,{})):
                self.assertTrue(native.collect('fixture',500))
            self.assertEqual([name for name,_ in calls],['prepare','collect-appen','prepare-backup'])
            self.assertEqual(calls[-1][1],500-daily.PUBLICATION_SECONDS)

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
                if name=='publish-rws':self.assertGreaterEqual(deadline-clock[0],10)
                if name=='publish-rws':clock[0]=deadline;raise TimeoutError()
                clock[0]+=1
            with patch.object(cli.time,'monotonic',side_effect=lambda:clock[0]),patch.object(native,'phase',side_effect=phase):
                with self.assertRaises(TimeoutError):native.publish('fixture',240)
            self.assertEqual(calls,['backup','publish-rws'])
            self.assertLess(clock[0],240)

    def test_actual_fourteen_source_weights_reclaim_small_source_time_within_same_cap(self):
        # September 28's measured durations include six interrupted publishers;
        # they test allocation only, not successful real publication throughput.
        weights=dict(alignerr=5624,appen=32,dataannotation=10,dataforce=8,handshake=117,
            mercor=439,meridial=832,mindrift=141,oneforma=457,outlier=8,rws=42,
            surge=7,turing=251,welocalize=434)
        elapsed=dict(alignerr=75.091,appen=3.397,dataannotation=4.244,dataforce=3.694,
            handshake=5.5,mercor=9.822,meridial=15.084,mindrift=4.902,oneforma=9.564,
            outlier=3.19,rws=2.689,surge=4.396,turing=8.31,welocalize=9.413)
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'runs/fixture';target.mkdir(parents=True)
            daily.write_json(target/'publication-sources.json',list(reversed(weights)))
            daily.write_json(target/'publication-weights.json',weights)
            native=cli.NativeOperations(dict(state_directory=temp),'fixture')
            clock=[0];caps={};order=[]
            def phase(run_id,name,deadline):
                self.assertLessEqual(deadline,240);self.assertGreater(deadline,clock[0])
                if name=='backup':
                    self.assertEqual(deadline,60);clock[0]+=30.877;return
                if name=='finish':clock[0]+=2.286;return
                source=name.removeprefix('publish-');order.append(source)
                caps[source]=deadline-clock[0]
                self.assertLessEqual(deadline,210)
                self.assertGreaterEqual(caps[source],10)
                clock[0]+=elapsed[source]
            with patch.object(cli.time,'monotonic',side_effect=lambda:clock[0]),patch.object(native,'phase',side_effect=phase):
                native.publish('fixture',240)
            self.assertEqual(order,sorted(weights,key=lambda source:(weights[source],source)))
            self.assertEqual(len(set(order)),14)
            self.assertGreater(caps['alignerr'],90)
            self.assertGreater(caps['handshake'],11)
            self.assertGreater(caps['mercor'],15)
            self.assertGreater(caps['meridial'],21)
            self.assertLess(clock[0],240)
            recorded=daily.read_json(target/'publication-allocation.json')
            self.assertEqual(recorded['ordered_sources'],order)
            self.assertEqual(recorded['phase_caps_seconds'],{source:round(cap,3) for source,cap in caps.items()})

    def test_expanded_dataforce_has_useful_time_in_full_daily_publication_pool(self):
        # The Sep 28 daily run gave 32 rich DataForce records only 10.283 s.
        # Interpreter startup plus the rollback reserve left too little time
        # for mandatory evidence replay. A targeted two-source run hid this.
        weights=dict(alignerr=5624,appen=26,dataannotation=10,dataforce=32,handshake=117,
            mercor=373,meridial=832,mindrift=92,oneforma=457,outlier=8,rws=42,
            surge=7,turing=248,welocalize=390)
        elapsed=dict(alignerr=71.512,appen=3.141,dataannotation=3.695,dataforce=9.5,
            handshake=5.5,mercor=9.917,meridial=18.05,mindrift=4.902,oneforma=11.993,
            outlier=4.448,rws=2.738,surge=6.457,turing=7.158,welocalize=9.664)
        # Cover the observed 13-source run and tomorrow's 14-source run once
        # Mindrift is eligible. Durations simulate allocation, not a live crawl.
        for deferred in (True,False):
            selected={s:n for s,n in weights.items() if not (deferred and s=='mindrift')}
            with self.subTest(deferred=deferred),tempfile.TemporaryDirectory() as temp:
                target=Path(temp)/'runs/fixture';target.mkdir(parents=True)
                daily.write_json(target/'publication-sources.json',list(selected))
                daily.write_json(target/'publication-weights.json',selected)
                native=cli.NativeOperations(dict(state_directory=temp),'fixture')
                clock=[0];caps={}
                def phase(run_id,name,deadline):
                    self.assertLessEqual(deadline,237.544)  # original 2.456 s stop deduction
                    if name=='backup':clock[0]+=26.493;return
                    if name=='finish':clock[0]+=3.696;return
                    source=name.removeprefix('publish-');cap=deadline-clock[0]
                    caps[source]=cap
                    self.assertLess(elapsed[source],cap)
                    clock[0]+=elapsed[source]
                with patch.object(cli.time,'monotonic',side_effect=lambda:clock[0]),patch.object(native,'phase',side_effect=phase):
                    native.publish('fixture',237.544)
                self.assertEqual(set(caps),set(selected))
                remaining=caps['dataforce']-1  # bounded interpreter/import allowance
                self.assertGreaterEqual(remaining-min(3,remaining/4),10)
                self.assertGreater(caps['alignerr'],elapsed['alignerr']+3)
                self.assertLess(clock[0],237.544)

    def test_publication_rejects_duplicate_or_invalid_source_allocations(self):
        cases=[(['appen','appen'],dict(appen=1)),(['unknown'],dict(unknown=1)),
            (['appen'],dict(rws=1)),(['appen'],dict(appen=True)),(['appen'],dict(appen=20_001))]
        for sources,weights in cases:
            with self.subTest(sources=sources,weights=weights),tempfile.TemporaryDirectory() as temp:
                target=Path(temp)/'runs/fixture';target.mkdir(parents=True)
                daily.write_json(target/'publication-sources.json',sources)
                daily.write_json(target/'publication-weights.json',weights)
                native=cli.NativeOperations(dict(state_directory=temp),'fixture')
                with patch.object(native,'phase') as phase:
                    with self.assertRaisesRegex(ValueError,'bounded_publication_weights_required'):
                        native.publish('fixture',240)
                self.assertEqual([call.args[1] for call in phase.call_args_list],['backup'])

    def test_publication_uncertain_database_stops_before_next_source(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'runs/fixture';target.mkdir(parents=True)
            database=Path(temp)/'fixture.sqlite3'
            daily.write_json(target/'publication-sources.json',['appen','rws'])
            daily.write_json(target/'publication-weights.json',dict(appen=1,rws=2))
            native=cli.NativeOperations(dict(state_directory=temp,database=str(database)),'fixture')
            calls=[]
            def phase(run_id,name,deadline):
                calls.append(name)
                if name=='publish-appen':
                    Path(str(database)+'-journal').touch()
                    raise RuntimeError('bounded_process_failed')
            with patch.object(native,'phase',side_effect=phase):
                with self.assertRaises(RuntimeError):native.publish('fixture',240)
            self.assertEqual(calls,['backup','publish-appen'])

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
